"""
Enable / disable tools on a North deployment, at both the
**admin** (deployment-wide) and **user** (per-account) levels.

Two flavors of tool, two layers of management
---------------------------------------------
On North, "tools" come in two flavors:

1. **Built-in hosted tools** (e.g. ``tavily_web_search``,
   ``toolkit_calculator``, ``web_scrape``). These are baked into
   the deployment's catalog at startup. There is **no API** to
   toggle them at runtime -- they're either compiled in or they
   aren't. To list them: ``catalog`` subcommand.

2. **OAuth-backed connector tools** (Gmail, Google Drive, Slack,
   OneDrive, Jira, MCP servers, etc.). These have two layers:

   a) **Admin / deployment level** -- a connector record exists
      in North's DB, and the underlying OAuth client (client_id,
      client_secret, scopes, redirect URI) is registered in Dex
      (the platform's identity layer) via k8s/Helm config. The
      admin POSTs ``/internal/v1/admin/connectors`` to register
      a connector once Dex already knows about it. There's no
      "enabled" boolean -- "enable" means register, "disable"
      means delete. Requires the ``can_configure_mcp_servers``
      permission.

   b) **Per-user level** -- each user grants OAuth consent
      individually. Once a connector exists at the deployment
      level, a user "enables" it for themselves by visiting the
      ``auth_url`` and completing consent in a browser, and
      "disables" it by deleting their dex token via
      ``/internal/v1/connectors/{id}/disconnect``.

Important caveat about admin connector creation
-----------------------------------------------
The admin POST endpoint only *registers* a connector in North's
DB. The actual OAuth client config (client_id, client_secret,
allowed scopes, redirect URIs) lives in **Dex's config, which is
managed via k8s/Helm**. ``POST /internal/v1/admin/connectors``
will fail or behave incorrectly if Dex doesn't already know about
the connector. So the admin enable flow is:

    1. Add the OAuth provider in Dex (Helm values / k8s config)
    2. Re-deploy / reconfigure Dex
    3. ``admin-add-connector <id> --name X --description Y``
       to register it in North

OAuth Apps (separate concept)
-----------------------------
There's also a separate admin-managed object: **OAuth Apps**.
These are app-level OAuth clients that custom integrations use
to call North on a user's behalf. They are fully manageable via
API (no Dex/k8s dependency):

    admin-oauth-apps-list
    admin-oauth-app-add <id> --name N --description D --scope ...
    admin-oauth-app-update <id> [--name N] [--description D] [--scope ...]
    admin-oauth-app-remove <id>
    admin-scopes                    # list available scopes

Subcommands at a glance
-----------------------
Per-user (no special permission required):
  ``catalog``                        every hosted tool on the deployment
  ``list``                           every connector + your auth status
  ``status <connector_id>``          one connector's full record
  ``enable <connector_id> [--open]`` get OAuth URL for current user
  ``disable <connector_id>``         disconnect current user
  ``revoke-tool <tool_id>``          revoke single tool's per-user auth

Admin (require ``can_configure_mcp_servers``):
  ``admin-connectors-list``
  ``admin-add-connector <id> --name N --description D``
  ``admin-update-connector <id> [--name N] [--description D]``
  ``admin-remove-connector <id>``

  ``admin-oauth-apps-list``
  ``admin-oauth-app <id>``
  ``admin-oauth-app-add <id> --name N --description D --scope ...``
  ``admin-oauth-app-update <id> [--name N] [--description D] [--scope ...]``
  ``admin-oauth-app-remove <id>``
  ``admin-scopes``

Usage
-----
    python examples/north_manage_tools.py list \\
        --base-url https://kuvbi.democloud.cohere.com/api \\
        --email   you@example.com --password '...'

    python examples/north_manage_tools.py admin-connectors-list ...
    python examples/north_manage_tools.py admin-add-connector google_drive \\
        --name "Google Drive" --description "Search Google Drive files" ...
    python examples/north_manage_tools.py admin-remove-connector gmail   ...

You can pass ``--token <jwt>`` instead of email/password.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import webbrowser
from pathlib import Path

import httpx


def _strip_quotes(value: str) -> str:
    if (
        len(value) >= 2
        and value[0] == value[-1]
        and value[0] in ("'", '"')
    ):
        return value[1:-1]
    return value


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = _strip_quotes(value.strip())


def _signin(
    *, base_url: str, email: str, password: str, timeout: float = 30.0
) -> str:
    url = f"{base_url.rstrip('/')}/v1/signin"
    r = httpx.post(
        url, json={"email": email, "password": password}, timeout=timeout
    )
    if r.status_code >= 400:
        raise SystemExit(f"Signin failed ({r.status_code}): {r.text}")
    token = r.json().get("token")
    if not token:
        raise SystemExit(f"Signin returned no token: {r.text}")
    return token


def _resolve_token(args: argparse.Namespace) -> tuple[str, str]:
    """Return (jwt, base_url). Raises SystemExit if creds are missing."""
    if not args.base_url:
        raise SystemExit(
            "No base URL configured. Set NORTH_BASE_URL in .env "
            "or pass --base-url. .env is the single source of truth."
        )
    base_url = args.base_url.rstrip("/")
    if args.token:
        return args.token, base_url
    if not args.email or not args.password:
        raise SystemExit(
            "Need either --token, or --email + --password (or "
            "NORTH_EMAIL / NORTH_PASSWORD env vars)."
        )
    return (
        _signin(
            base_url=base_url, email=args.email, password=args.password
        ),
        base_url,
    )


def _auth_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# API wrappers
# ---------------------------------------------------------------------------


def list_connectors(
    *,
    base_url: str,
    token: str,
    hide_first_party: bool = False,
    timeout: float = 30.0,
) -> list[dict]:
    """GET /internal/v1/connectors -- every connector + auth status.

    Each entry includes:
      - id, name, description
      - tools: list of tool names this connector provides
      - auth_url: a string when you're NOT authed, None when you ARE
    """
    url = f"{base_url.rstrip('/')}/internal/v1/connectors"
    r = httpx.get(
        url,
        headers=_auth_headers(token),
        params={"hideFirstParty": str(hide_first_party).lower()},
        timeout=timeout,
    )
    if r.status_code >= 400:
        raise SystemExit(
            f"List connectors failed ({r.status_code}): {r.text}"
        )
    return r.json() or []


def list_hosted_tools(
    *, base_url: str, token: str, timeout: float = 30.0
) -> list[dict]:
    url = f"{base_url.rstrip('/')}/internal/v1/tools"
    r = httpx.get(
        url, headers=_auth_headers(token), timeout=timeout
    )
    if r.status_code >= 400:
        raise SystemExit(f"List tools failed ({r.status_code}): {r.text}")
    return r.json() or []


def get_connector_auth_url(
    *,
    base_url: str,
    token: str,
    connector_id: str,
    timeout: float = 30.0,
) -> str | None:
    """GET /internal/v1/connectors/auth_url/{connector_id} -- start OAuth."""
    url = (
        f"{base_url.rstrip('/')}/internal/v1/connectors/"
        f"auth_url/{connector_id}"
    )
    r = httpx.get(url, headers=_auth_headers(token), timeout=timeout)
    if r.status_code >= 400:
        raise SystemExit(
            f"Get auth URL failed ({r.status_code}): {r.text}"
        )
    body = r.json() or {}
    return body.get("url")


def disconnect_connector(
    *,
    base_url: str,
    token: str,
    connector_id: str,
    timeout: float = 30.0,
) -> dict:
    """DELETE /internal/v1/connectors/{id}/disconnect -- revoke for me."""
    url = (
        f"{base_url.rstrip('/')}/internal/v1/connectors/"
        f"{connector_id}/disconnect"
    )
    r = httpx.delete(
        url, headers=_auth_headers(token), timeout=timeout
    )
    if r.status_code >= 400:
        raise SystemExit(
            f"Disconnect failed ({r.status_code}): {r.text}"
        )
    try:
        return r.json()
    except json.JSONDecodeError:
        return {"raw": r.text}


def revoke_tool_auth(
    *,
    base_url: str,
    token: str,
    tool_id: str,
    timeout: float = 30.0,
) -> dict:
    """DELETE /internal/v1/tool/auth/{tool_id} -- revoke single tool auth."""
    url = (
        f"{base_url.rstrip('/')}/internal/v1/tool/auth/{tool_id}"
    )
    r = httpx.delete(
        url, headers=_auth_headers(token), timeout=timeout
    )
    if r.status_code >= 400:
        raise SystemExit(
            f"Revoke tool auth failed ({r.status_code}): {r.text}"
        )
    try:
        return r.json()
    except json.JSONDecodeError:
        return {"raw": r.text}


# ---------------------------------------------------------------------------
# Admin API wrappers
# ---------------------------------------------------------------------------


def _request_json(
    method: str,
    url: str,
    *,
    token: str,
    json_body: dict | None = None,
    timeout: float = 30.0,
) -> dict | list:
    r = httpx.request(
        method,
        url,
        headers=_auth_headers(token),
        json=json_body,
        timeout=timeout,
    )
    if r.status_code == 403:
        raise SystemExit(
            f"403 Forbidden at {url}. Your account likely lacks the "
            "required permission (typically 'can_configure_mcp_servers'). "
            f"Server said: {r.text}"
        )
    if r.status_code >= 400:
        raise SystemExit(
            f"{method} {url} failed ({r.status_code}): {r.text}"
        )
    if r.status_code == 204 or not r.content:
        return {}
    try:
        return r.json()
    except json.JSONDecodeError:
        return {"raw": r.text}


def admin_list_connectors(
    *, base_url: str, token: str
) -> list[dict]:
    """GET /internal/v1/admin/connectors -- admin view, all connectors."""
    return _request_json(
        "GET",
        f"{base_url.rstrip('/')}/internal/v1/admin/connectors",
        token=token,
    )  # type: ignore[return-value]


def admin_create_connector(
    *,
    base_url: str,
    token: str,
    connector_id: str,
    name: str,
    description: str,
) -> dict:
    """POST /internal/v1/admin/connectors -- register a connector.

    NOTE: This only adds a record in North's DB. The OAuth client
    behind it MUST already be configured in Dex via k8s/Helm; this
    endpoint does not provision the OAuth credentials.
    """
    return _request_json(
        "POST",
        f"{base_url.rstrip('/')}/internal/v1/admin/connectors",
        token=token,
        json_body={
            "id": connector_id,
            "name": name,
            "description": description,
        },
    )  # type: ignore[return-value]


def admin_update_connector(
    *,
    base_url: str,
    token: str,
    connector_id: str,
    name: str | None = None,
    description: str | None = None,
) -> dict:
    """PATCH /internal/v1/admin/connectors/{id} -- only name/description."""
    body = {}
    if name is not None:
        body["name"] = name
    if description is not None:
        body["description"] = description
    if not body:
        raise SystemExit("Nothing to update; pass --name and/or --description.")
    return _request_json(
        "PATCH",
        f"{base_url.rstrip('/')}/internal/v1/admin/connectors/{connector_id}",
        token=token,
        json_body=body,
    )  # type: ignore[return-value]


def admin_delete_connector(
    *, base_url: str, token: str, connector_id: str
) -> dict:
    """DELETE /internal/v1/admin/connectors/{id} -- remove the connector."""
    return _request_json(
        "DELETE",
        f"{base_url.rstrip('/')}/internal/v1/admin/connectors/{connector_id}",
        token=token,
    )  # type: ignore[return-value]


def admin_list_oauth_apps(
    *, base_url: str, token: str
) -> list[dict]:
    return _request_json(
        "GET",
        f"{base_url.rstrip('/')}/internal/v1/admin/oauth-apps",
        token=token,
    )  # type: ignore[return-value]


def admin_get_oauth_app(
    *, base_url: str, token: str, oauth_app_id: str
) -> dict:
    return _request_json(
        "GET",
        f"{base_url.rstrip('/')}/internal/v1/admin/oauth-apps/{oauth_app_id}",
        token=token,
    )  # type: ignore[return-value]


def admin_create_oauth_app(
    *,
    base_url: str,
    token: str,
    oauth_app_id: str,
    name: str,
    description: str,
    allowed_scopes: list[str],
) -> dict:
    return _request_json(
        "POST",
        f"{base_url.rstrip('/')}/internal/v1/admin/oauth-apps",
        token=token,
        json_body={
            "id": oauth_app_id,
            "name": name,
            "description": description,
            "allowed_scopes": allowed_scopes,
        },
    )  # type: ignore[return-value]


def admin_update_oauth_app(
    *,
    base_url: str,
    token: str,
    oauth_app_id: str,
    name: str | None = None,
    description: str | None = None,
    allowed_scopes: list[str] | None = None,
) -> dict:
    body: dict = {}
    if name is not None:
        body["name"] = name
    if description is not None:
        body["description"] = description
    if allowed_scopes is not None:
        body["allowed_scopes"] = allowed_scopes
    if not body:
        raise SystemExit(
            "Nothing to update; pass --name, --description, or --scope."
        )
    return _request_json(
        "PATCH",
        f"{base_url.rstrip('/')}/internal/v1/admin/oauth-apps/{oauth_app_id}",
        token=token,
        json_body=body,
    )  # type: ignore[return-value]


def admin_delete_oauth_app(
    *, base_url: str, token: str, oauth_app_id: str
) -> dict:
    return _request_json(
        "DELETE",
        f"{base_url.rstrip('/')}/internal/v1/admin/oauth-apps/{oauth_app_id}",
        token=token,
    )  # type: ignore[return-value]


def admin_list_scopes(
    *, base_url: str, token: str
) -> list[dict]:
    return _request_json(
        "GET",
        f"{base_url.rstrip('/')}/internal/v1/admin/oauth-apps/scopes",
        token=token,
    )  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# CLI handlers
# ---------------------------------------------------------------------------


def _print_connector_row(c: dict) -> None:
    auth_url = c.get("auth_url")
    status = "AUTHED" if not auth_url else "NEEDS-AUTH"
    cid = c.get("id", "<no-id>")
    name = c.get("name", "<no-name>")
    tools = c.get("tools") or []
    print(
        f"  [{status:10}] {cid:25} {name:30} "
        f"tools={tools}"
    )


def cmd_list(args: argparse.Namespace) -> int:
    token, base_url = _resolve_token(args)
    connectors = list_connectors(
        base_url=base_url,
        token=token,
        hide_first_party=args.hide_first_party,
    )
    print(f"Found {len(connectors)} connector(s):")
    if not connectors:
        print(
            "  (none -- this deployment has no OAuth-backed "
            "connectors configured, or your account has no "
            "visibility on any.)"
        )
        return 0
    for c in connectors:
        _print_connector_row(c)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    token, base_url = _resolve_token(args)
    connectors = list_connectors(base_url=base_url, token=token)
    match = next(
        (c for c in connectors if c.get("id") == args.connector_id), None
    )
    if not match:
        print(
            f"Connector {args.connector_id!r} not found. "
            f"Known ids: {[c.get('id') for c in connectors]}"
        )
        return 2
    print(json.dumps(match, indent=2))
    return 0


def cmd_enable(args: argparse.Namespace) -> int:
    """Print (and optionally open) the OAuth URL for a connector."""
    token, base_url = _resolve_token(args)
    auth_url = get_connector_auth_url(
        base_url=base_url,
        token=token,
        connector_id=args.connector_id,
    )
    if not auth_url:
        # The /auth_url endpoint can return null when the user is
        # already authenticated; double-check via /connectors.
        conns = list_connectors(base_url=base_url, token=token)
        match = next(
            (c for c in conns if c.get("id") == args.connector_id), None
        )
        if match and not match.get("auth_url"):
            print(
                f"Connector {args.connector_id!r} is already "
                f"authenticated for this user. Nothing to do."
            )
            return 0
        print(
            f"Got no auth URL for {args.connector_id!r}. "
            f"Connector record: {match!r}"
        )
        return 1

    print("To finish enabling this connector, open the following URL")
    print("in a browser and complete the OAuth consent screen:")
    print()
    print(f"  {auth_url}")
    print()
    if args.open:
        opened = webbrowser.open(auth_url)
        if opened:
            print("(Opened in your default browser.)")
        else:
            print(
                "(Could not auto-open browser; copy the URL above.)"
            )
    print(
        "After consent, re-run `list` or `status` to confirm "
        "auth_url has flipped to null."
    )
    return 0


def cmd_disable(args: argparse.Namespace) -> int:
    token, base_url = _resolve_token(args)
    result = disconnect_connector(
        base_url=base_url,
        token=token,
        connector_id=args.connector_id,
    )
    print(
        f"Disconnected {args.connector_id!r} for the current user."
    )
    print(json.dumps(result, indent=2))
    return 0


def cmd_revoke_tool(args: argparse.Namespace) -> int:
    token, base_url = _resolve_token(args)
    result = revoke_tool_auth(
        base_url=base_url, token=token, tool_id=args.tool_id
    )
    print(f"Revoked auth for tool {args.tool_id!r}.")
    print(json.dumps(result, indent=2))
    return 0


def cmd_auth_tool(args: argparse.Namespace) -> int:
    """Get/open the OAuth URL for a hosted tool (gmail, google_drive...).

    Hosted-tool OAuth (callback /internal/v1/tool/auth) is a different
    pathway from connector OAuth (callback /internal/v1/connectors/
    oauth/callback). Use this subcommand for tools that aren't
    registered as Dex connectors on the deployment -- otherwise
    `enable` hits Dex and gets "Failed to initialize connector".

    The auth URL is embedded in /internal/v1/tools as
    ``required_auths[].url`` and is user-scoped (state is encrypted
    with the caller's user_id), so we re-fetch it as the user.
    """
    token, base_url = _resolve_token(args)
    tools = list_hosted_tools(base_url=base_url, token=token)
    match = next(
        (t for t in tools if t.get("name") == args.tool_id), None
    )
    if not match:
        print(
            f"Tool {args.tool_id!r} not found. Known tool ids: "
            f"{[t.get('name') for t in tools]}"
        )
        return 2

    auths = match.get("required_auths") or []
    if not auths:
        print(
            f"Tool {args.tool_id!r} requires no OAuth (it's a "
            f"built-in tool). Nothing to do."
        )
        return 0

    auth_url = auths[0].get("url")
    if not auth_url:
        print(
            f"Tool {args.tool_id!r} has required_auths but no URL "
            f"-- the user is likely already authenticated. Record: "
            f"{auths[0]!r}"
        )
        return 0

    print("To authenticate this tool for the current user, open the")
    print("following URL in a browser and complete the OAuth consent:")
    print()
    print(f"  {auth_url}")
    print()
    if args.open:
        opened = webbrowser.open(auth_url)
        if opened:
            print("(Opened in your default browser.)")
        else:
            print(
                "(Could not open a browser automatically; copy the "
                "URL above into one manually.)"
            )
    print(
        "After consent, re-run `catalog` -- the tool's "
        "required_auths should be empty."
    )
    return 0


def cmd_catalog(args: argparse.Namespace) -> int:
    """Show every hosted tool on the deployment + which need OAuth."""
    token, base_url = _resolve_token(args)
    tools = list_hosted_tools(base_url=base_url, token=token)
    print(f"Found {len(tools)} hosted tool(s) on this deployment:")
    for t in tools:
        name = t.get("name") or "<no-name>"
        disp = t.get("display_name") or ""
        auth_req = bool(
            t.get("is_auth_required") or t.get("required_auths")
        )
        flag = "OAUTH" if auth_req else "built-in"
        print(
            f"  [{flag:8}] {name:30} {disp!r:35} "
            f"required_auths={t.get('required_auths') or []}"
        )
    print()
    print(
        "Note: built-in tools cannot be enabled/disabled via the "
        "API; they're baked into the deployment. Use `list` / "
        "`enable` / `disable` for OAuth tools."
    )
    return 0


# ---------------------------------------------------------------------------
# Admin command handlers
# ---------------------------------------------------------------------------


def cmd_admin_list_connectors(args: argparse.Namespace) -> int:
    token, base_url = _resolve_token(args)
    conns = admin_list_connectors(base_url=base_url, token=token)
    print(f"Admin view: {len(conns)} connector(s) registered:")
    if not conns:
        print(
            "  (none -- no connectors are registered on this "
            "deployment yet.)"
        )
        return 0
    for c in conns:
        cid = c.get("id", "<no-id>")
        name = c.get("name", "<no-name>")
        desc = c.get("description", "")
        tools = c.get("tools") or []
        print(f"  - id={cid:25} name={name!r}")
        print(f"      description : {desc!r}")
        print(f"      tools       : {tools}")
    return 0


def cmd_admin_add_connector(args: argparse.Namespace) -> int:
    token, base_url = _resolve_token(args)
    print(
        "WARNING: this only registers the connector in North's DB. "
        "The underlying OAuth client must already be configured in "
        "Dex (k8s/Helm). If Dex doesn't know about this connector "
        "id, users won't be able to authenticate against it."
    )
    print()
    result = admin_create_connector(
        base_url=base_url,
        token=token,
        connector_id=args.connector_id,
        name=args.name,
        description=args.description,
    )
    print(f"Created connector {args.connector_id!r}:")
    print(json.dumps(result, indent=2))
    return 0


def cmd_admin_update_connector(args: argparse.Namespace) -> int:
    token, base_url = _resolve_token(args)
    result = admin_update_connector(
        base_url=base_url,
        token=token,
        connector_id=args.connector_id,
        name=args.name,
        description=args.description,
    )
    print(f"Updated connector {args.connector_id!r}:")
    print(json.dumps(result, indent=2))
    return 0


def cmd_admin_remove_connector(args: argparse.Namespace) -> int:
    token, base_url = _resolve_token(args)
    if not args.yes:
        confirm = input(
            f"Really delete connector {args.connector_id!r}? "
            "All users' auth state for this connector will be lost. "
            "Type 'yes' to confirm: "
        )
        if confirm.strip().lower() != "yes":
            print("Aborted.")
            return 1
    result = admin_delete_connector(
        base_url=base_url, token=token, connector_id=args.connector_id
    )
    print(f"Deleted connector {args.connector_id!r}.")
    if result:
        print(json.dumps(result, indent=2))
    return 0


def cmd_admin_list_oauth_apps(args: argparse.Namespace) -> int:
    token, base_url = _resolve_token(args)
    apps = admin_list_oauth_apps(base_url=base_url, token=token)
    print(f"Found {len(apps)} OAuth app(s):")
    for a in apps:
        print(
            f"  - id={a.get('id'):25} "
            f"name={a.get('name')!r:25} "
            f"scopes={a.get('allowed_scopes') or []}"
        )
    return 0


def cmd_admin_get_oauth_app(args: argparse.Namespace) -> int:
    token, base_url = _resolve_token(args)
    app = admin_get_oauth_app(
        base_url=base_url, token=token, oauth_app_id=args.oauth_app_id
    )
    print(json.dumps(app, indent=2))
    return 0


def cmd_admin_add_oauth_app(args: argparse.Namespace) -> int:
    token, base_url = _resolve_token(args)
    result = admin_create_oauth_app(
        base_url=base_url,
        token=token,
        oauth_app_id=args.oauth_app_id,
        name=args.name,
        description=args.description,
        allowed_scopes=args.scope or [],
    )
    print(f"Created OAuth app {args.oauth_app_id!r}:")
    print(json.dumps(result, indent=2))
    return 0


def cmd_admin_update_oauth_app(args: argparse.Namespace) -> int:
    token, base_url = _resolve_token(args)
    result = admin_update_oauth_app(
        base_url=base_url,
        token=token,
        oauth_app_id=args.oauth_app_id,
        name=args.name,
        description=args.description,
        allowed_scopes=args.scope,
    )
    print(f"Updated OAuth app {args.oauth_app_id!r}:")
    print(json.dumps(result, indent=2))
    return 0


def cmd_admin_remove_oauth_app(args: argparse.Namespace) -> int:
    token, base_url = _resolve_token(args)
    if not args.yes:
        confirm = input(
            f"Really delete OAuth app {args.oauth_app_id!r}? "
            "Any integrations using its credentials will break. "
            "Type 'yes' to confirm: "
        )
        if confirm.strip().lower() != "yes":
            print("Aborted.")
            return 1
    result = admin_delete_oauth_app(
        base_url=base_url,
        token=token,
        oauth_app_id=args.oauth_app_id,
    )
    print(f"Deleted OAuth app {args.oauth_app_id!r}.")
    if result:
        print(json.dumps(result, indent=2))
    return 0


def cmd_admin_scopes(args: argparse.Namespace) -> int:
    token, base_url = _resolve_token(args)
    scopes = admin_list_scopes(base_url=base_url, token=token)
    print(f"Found {len(scopes)} OAuth scope(s):")
    for s in scopes:
        name = s.get("name", "<no-name>")
        route = s.get("route") or ""
        print(f"  - {name:30} route={route!r}")
    return 0


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def _add_auth_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--base-url",
        default=os.environ.get("NORTH_BASE_URL"),
        help=(
            "North API base URL. Defaults to NORTH_BASE_URL from "
            ".env / environment."
        ),
    )
    p.add_argument(
        "--email",
        default=os.environ.get("NORTH_EMAIL"),
        help="Email of the user. Or set NORTH_EMAIL.",
    )
    p.add_argument(
        "--password",
        default=os.environ.get("NORTH_PASSWORD"),
        help="Password. Or set NORTH_PASSWORD.",
    )
    p.add_argument(
        "--token",
        default=os.environ.get("NORTH_API_TOKEN"),
        help=(
            "Skip /v1/signin and use this JWT directly. Defaults "
            "to the NORTH_API_TOKEN env var (or .env entry)."
        ),
    )


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent.parent
    _load_dotenv(repo_root / ".env")

    parser = argparse.ArgumentParser(
        description=(
            "Enable / disable per-user OAuth-backed tools "
            "(Gmail, Google Drive, etc.) on a North deployment."
        )
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser(
        "list",
        help="List every connector visible to the user, with auth status.",
    )
    p_list.add_argument(
        "--hide-first-party",
        action="store_true",
        help=(
            "Hide first-party connectors (matches the UI's hideFirstParty "
            "behavior). Off by default."
        ),
    )
    _add_auth_args(p_list)
    p_list.set_defaults(handler=cmd_list)

    p_status = sub.add_parser(
        "status", help="Show one connector's auth status."
    )
    p_status.add_argument("connector_id")
    _add_auth_args(p_status)
    p_status.set_defaults(handler=cmd_status)

    p_enable = sub.add_parser(
        "enable",
        help=(
            "Get the OAuth URL to enable a connector for the current "
            "user. Prints the URL; optionally opens it in a browser."
        ),
    )
    p_enable.add_argument("connector_id")
    p_enable.add_argument(
        "--open",
        action="store_true",
        help="Try to open the auth URL in the default browser.",
    )
    _add_auth_args(p_enable)
    p_enable.set_defaults(handler=cmd_enable)

    p_disable = sub.add_parser(
        "disable",
        help=(
            "Disconnect the current user from a connector "
            "(deletes their dex token; forces re-auth next use)."
        ),
    )
    p_disable.add_argument("connector_id")
    _add_auth_args(p_disable)
    p_disable.set_defaults(handler=cmd_disable)

    p_revoke = sub.add_parser(
        "revoke-tool",
        help=(
            "Revoke per-user auth for a single tool by its tool name "
            "(e.g. 'gmail', 'google_drive')."
        ),
    )
    p_revoke.add_argument("tool_id")
    _add_auth_args(p_revoke)
    p_revoke.set_defaults(handler=cmd_revoke_tool)

    p_auth_tool = sub.add_parser(
        "auth-tool",
        help=(
            "Get the OAuth URL to authenticate a hosted tool (gmail, "
            "google_drive, ...) for the current user. Use this when "
            "the tool isn't a Dex connector and `enable` fails with "
            "'Failed to initialize connector'."
        ),
    )
    p_auth_tool.add_argument(
        "tool_id",
        help="Hosted tool name (e.g. 'gmail', 'google_drive').",
    )
    p_auth_tool.add_argument(
        "--open",
        action="store_true",
        help="Try to open the auth URL in the default browser.",
    )
    _add_auth_args(p_auth_tool)
    p_auth_tool.set_defaults(handler=cmd_auth_tool)

    p_catalog = sub.add_parser(
        "catalog",
        help=(
            "List every hosted tool on the deployment, marking which "
            "ones need OAuth. Useful for figuring out tool ids."
        ),
    )
    _add_auth_args(p_catalog)
    p_catalog.set_defaults(handler=cmd_catalog)

    # ---- admin: connectors ----
    p_alc = sub.add_parser(
        "admin-connectors-list",
        help=(
            "[admin] List every connector registered on the "
            "deployment. Requires can_configure_mcp_servers."
        ),
    )
    _add_auth_args(p_alc)
    p_alc.set_defaults(handler=cmd_admin_list_connectors)

    p_aac = sub.add_parser(
        "admin-add-connector",
        help=(
            "[admin] Register a new connector. NOTE: Dex must "
            "already be configured for this connector id."
        ),
    )
    p_aac.add_argument("connector_id")
    p_aac.add_argument("--name", required=True)
    p_aac.add_argument("--description", required=True)
    _add_auth_args(p_aac)
    p_aac.set_defaults(handler=cmd_admin_add_connector)

    p_auc = sub.add_parser(
        "admin-update-connector",
        help="[admin] Update a connector's name and/or description.",
    )
    p_auc.add_argument("connector_id")
    p_auc.add_argument("--name", default=None)
    p_auc.add_argument("--description", default=None)
    _add_auth_args(p_auc)
    p_auc.set_defaults(handler=cmd_admin_update_connector)

    p_arc = sub.add_parser(
        "admin-remove-connector",
        help=(
            "[admin] Delete a connector from the deployment. "
            "All users' auth state for this connector is lost."
        ),
    )
    p_arc.add_argument("connector_id")
    p_arc.add_argument(
        "--yes", action="store_true", help="Skip confirmation prompt."
    )
    _add_auth_args(p_arc)
    p_arc.set_defaults(handler=cmd_admin_remove_connector)

    # ---- admin: oauth apps ----
    p_aol = sub.add_parser(
        "admin-oauth-apps-list",
        help="[admin] List every OAuth app registered.",
    )
    _add_auth_args(p_aol)
    p_aol.set_defaults(handler=cmd_admin_list_oauth_apps)

    p_aog = sub.add_parser(
        "admin-oauth-app",
        help="[admin] Show one OAuth app's full record.",
    )
    p_aog.add_argument("oauth_app_id")
    _add_auth_args(p_aog)
    p_aog.set_defaults(handler=cmd_admin_get_oauth_app)

    p_aoa = sub.add_parser(
        "admin-oauth-app-add",
        help="[admin] Create a new OAuth app.",
    )
    p_aoa.add_argument("oauth_app_id")
    p_aoa.add_argument("--name", required=True)
    p_aoa.add_argument("--description", required=True)
    p_aoa.add_argument(
        "--scope",
        action="append",
        help=(
            "An allowed scope. Repeat the flag for multiple scopes. "
            "Use `admin-scopes` to see what's available."
        ),
    )
    _add_auth_args(p_aoa)
    p_aoa.set_defaults(handler=cmd_admin_add_oauth_app)

    p_aou = sub.add_parser(
        "admin-oauth-app-update",
        help=(
            "[admin] Update name / description / allowed scopes "
            "on an existing OAuth app."
        ),
    )
    p_aou.add_argument("oauth_app_id")
    p_aou.add_argument("--name", default=None)
    p_aou.add_argument("--description", default=None)
    p_aou.add_argument(
        "--scope",
        action="append",
        default=None,
        help=(
            "Replace allowed_scopes with these values. Pass --scope "
            "once per scope. Omit to leave scopes unchanged."
        ),
    )
    _add_auth_args(p_aou)
    p_aou.set_defaults(handler=cmd_admin_update_oauth_app)

    p_aor = sub.add_parser(
        "admin-oauth-app-remove",
        help="[admin] Delete an OAuth app.",
    )
    p_aor.add_argument("oauth_app_id")
    p_aor.add_argument(
        "--yes", action="store_true", help="Skip confirmation prompt."
    )
    _add_auth_args(p_aor)
    p_aor.set_defaults(handler=cmd_admin_remove_oauth_app)

    p_as = sub.add_parser(
        "admin-scopes",
        help="[admin] List every OAuth scope the deployment exposes.",
    )
    _add_auth_args(p_as)
    p_as.set_defaults(handler=cmd_admin_scopes)

    args = parser.parse_args()
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
