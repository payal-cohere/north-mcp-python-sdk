"""
Fetch a North API JWT programmatically and (optionally) save it
to .env so the other example scripts can use it without prompting
for a password.

Two flows, one script
---------------------

**Regular user (default).** Uses the same path the ``/login`` UI
uses underneath: ``POST /v1/signin`` against the North API with
email + password. Works for any user that signed up via the North
``/register`` flow. Returns a token signed by North's own
``cohere-toolkit`` issuer, with whatever TTL the deployment is
configured for (typically 7-90 days).

**Admin user (``--admin`` flag).** Drives the Dex OIDC code-grant
flow that the ``/admin/`` UI uses. This is required because
**admin accounts live in Dex's local connector, not in North's
user table**, so ``/v1/signin`` will reject them with 401. The
flow:

  1. GET ``/admin/api/auth/login/local`` -> redirected to Dex
  2. POST ``login`` + ``password`` to Dex's local-connector form
  3. Dex redirects back to ``/admin/api/auth/callback?code=...``
  4. North admin app exchanges the code, sets ``admin_id_token``
     cookie -- this is the JWT we pull out

The resulting Dex JWT is issued by ``https://<host>/dex`` with
audience ``north-admin`` and a typical 24 h TTL. It works as a
Bearer token against the same ``/api/`` endpoints as the regular
token (including ``/api/internal/v1/admin/connectors``), so
downstream scripts don't need to know the difference.

Why this exists
---------------
North's UI has a ``/developer`` page that *displays* the JWT your
browser session already holds. There is no dedicated "create
developer token" endpoint -- the page just shows the same JWT.
This script bypasses the UI entirely:

  1. Read ``NORTH_EMAIL`` / ``NORTH_PASSWORD`` (or take
     ``--email`` / ``--password`` on the CLI).
  2. Call the right signin path (regular or admin OIDC).
  3. Print the JWT (default), or persist it.

Usage
-----
Print the token to stdout (good for piping):

    .venv/bin/python examples/north_get_token.py \\
        --base-url https://kuvbi.democloud.cohere.com/api \\
        --email me@example.com --password '...'

Get an **admin** token via Dex OIDC (note: still pass --base-url
with /api, the script derives the /admin/ origin from it):

    .venv/bin/python examples/north_get_token.py --admin \\
        --base-url https://kuvbi.democloud.cohere.com/api \\
        --email admin@cohere.com --password '...' \\
        --decode --write-env --quiet

Capture into a shell var without printing the password to history:

    export NORTH_API_TOKEN=$(.venv/bin/python examples/north_get_token.py)

Persist into ``.env`` so the other scripts (north_create_agent.py,
north_manage_tools.py, north_admin_connectors.py, etc.) just pick
it up automatically:

    .venv/bin/python examples/north_get_token.py --write-env

Show token TTL and claims for debugging:

    .venv/bin/python examples/north_get_token.py --decode

Notes on expiry
---------------
* Regular tokens: TTL is set per deployment via
  ``JWT_ACCESS_TOKEN_EXPIRY``. kuvbi: ~7 days. demo: ~90 days.
* Admin tokens (Dex): TTL is set in Dex's config (typically
  ``id_tokens_valid_for``). Often 24 h. Just re-run the script
  with ``--admin`` to refresh.

``--write-env`` is idempotent (replaces an existing
``NORTH_API_TOKEN=`` line rather than appending a duplicate),
so cron-friendly.
"""

from __future__ import annotations

import argparse
import base64
import html as _html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

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
    """Tiny .env loader. Existing env vars win."""
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


def signin(
    *,
    base_url: str,
    email: str,
    password: str,
    timeout: float = 30.0,
) -> str:
    """Regular user signin against ``POST /v1/signin``."""
    url = f"{base_url.rstrip('/')}/v1/signin"
    r = httpx.post(
        url,
        json={"email": email, "password": password},
        timeout=timeout,
    )
    if r.status_code >= 400:
        raise SystemExit(
            f"Signin failed ({r.status_code}) at {url}:\n{r.text}"
        )
    token = r.json().get("token")
    if not token:
        raise SystemExit(f"Signin returned no token: {r.text}")
    return token


def signin_admin_oidc(
    *,
    base_url: str,
    email: str,
    password: str,
    connector_id: str = "local",
    timeout: float = 30.0,
) -> str:
    """Drive the Dex OIDC code grant used by the ``/admin/`` UI.

    Returns the ``admin_id_token`` JWT (issued by Dex, audience
    ``north-admin``), which is accepted as a Bearer by both the
    admin app at ``/admin/api/...`` and the main API at
    ``/api/internal/v1/admin/...``.

    Raises ``SystemExit`` with a clear message if Dex rejects the
    credentials, the form layout is unexpected, or the callback
    doesn't issue the expected cookie.

    Parameters
    ----------
    base_url : str
        The North API base, e.g. ``https://host/api``. The script
        derives the admin origin (``https://host``) and the admin
        login init endpoint (``/admin/api/auth/login/{connector_id}``)
        from this.
    connector_id : str
        The Dex connector to use. Defaults to ``"local"`` (Dex's
        built-in email + password backend, used for bootstrap admin
        accounts). Set to e.g. ``"google"`` if your tenant uses an
        external IdP -- but those flows can't be driven with raw
        password and require a browser.
    """
    parsed = urlparse(base_url)
    if not parsed.scheme or not parsed.netloc:
        raise SystemExit(f"Invalid base_url: {base_url!r}")
    origin = f"{parsed.scheme}://{parsed.netloc}"
    init_url = f"{origin}/admin/api/auth/login/{connector_id}"

    with httpx.Client(follow_redirects=True, timeout=timeout) as c:
        # 1) Hit the admin auth init -- httpx follows the chain
        #    through Dex until it lands on the login form page.
        r = c.get(init_url)
        if r.status_code >= 400:
            raise SystemExit(
                f"Admin OIDC init failed ({r.status_code}) at "
                f"{init_url}:\n{r.text[:500]}"
            )
        page = r.text
        landed_url = str(r.url)

        # 2) Parse the login form. Dex's local connector form has
        #    inputs named "login" and "password".
        m = re.search(r'<form[^>]+action="([^"]+)"', page)
        if not m:
            raise SystemExit(
                "Could not locate login form on Dex page at "
                f"{landed_url}. Body sample: {page[:300]!r}"
            )
        action = _html.unescape(m.group(1))
        if not action.startswith(("http://", "https://")):
            action = urljoin(landed_url, action)
        inputs = set(re.findall(r'<input[^>]+name="([^"]+)"', page))
        payload: dict = {}
        if "login" in inputs:
            payload["login"] = email
        elif "username" in inputs:
            payload["username"] = email
        else:
            raise SystemExit(
                f"Dex login form has no 'login' or 'username' "
                f"input; found inputs: {sorted(inputs)}"
            )
        payload["password"] = password

        # 3) Submit. Dex will either redirect through the callback
        #    (success) or re-render the form with an error banner
        #    (failure).
        r2 = c.post(action, data=payload)
        if r2.status_code >= 400:
            raise SystemExit(
                f"Dex login POST failed ({r2.status_code}): "
                f"{r2.text[:500]}"
            )
        # Detect "wrong credentials" -- Dex re-renders the form
        # with the same fields but adds an error message.
        body_lc = r2.text.lower()
        if (
            "<input" in body_lc
            and 'name="password"' in body_lc
            and ("invalid" in body_lc or "incorrect" in body_lc)
        ):
            raise SystemExit(
                "Dex rejected the email/password (rendered the "
                "login form again with an error). The credentials "
                "do not match any user in the Dex local connector."
            )

        # 4) The North admin app's /auth/callback handler should
        #    have set cookies including admin_id_token.
        admin_jwt = c.cookies.get("admin_id_token")
        if not admin_jwt:
            cookie_names = sorted({ck.name for ck in c.cookies.jar})
            raise SystemExit(
                "OIDC flow completed but no 'admin_id_token' "
                f"cookie was set. Cookies present: {cookie_names}. "
                "Final URL: " + str(r2.url)
            )
        return admin_jwt


def decode_jwt(token: str) -> dict | None:
    """Best-effort JWT payload decode (no signature verification)."""
    try:
        payload_b64 = token.split(".")[1]
    except IndexError:
        return None
    padding = "=" * (-len(payload_b64) % 4)
    try:
        return json.loads(
            base64.urlsafe_b64decode(payload_b64 + padding)
        )
    except (ValueError, json.JSONDecodeError):
        return None


def update_env_file(path: Path, key: str, value: str) -> str:
    """Set ``key=value`` in the .env at ``path``.

    If the file doesn't exist, creates it. If a line starting with
    ``key=`` (optionally prefixed with ``export ``, optionally
    commented out) already exists, replaces it. Otherwise appends.

    Returns one of "created", "replaced", "appended".
    """
    new_line = f"{key}={value}"
    if not path.exists():
        path.write_text(new_line + "\n")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return "created"

    lines = path.read_text().splitlines()
    out: list[str] = []
    replaced = False
    for raw in lines:
        line = raw.strip()
        # match  key=...  /  export key=...  /  # key=...  /  #export key=...
        stripped = line.lstrip("#").lstrip()
        if stripped.startswith("export "):
            stripped = stripped[len("export "):].lstrip()
        if "=" in stripped:
            head = stripped.split("=", 1)[0].strip()
            if head == key and not replaced:
                out.append(new_line)
                replaced = True
                continue
        out.append(raw)
    if not replaced:
        out.append(new_line)
    path.write_text("\n".join(out) + ("\n" if out else ""))
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return "replaced" if replaced else "appended"


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent.parent
    _load_dotenv(repo_root / ".env")

    parser = argparse.ArgumentParser(
        description=(
            "Fetch a North API JWT via /v1/signin (the same token "
            "the /developer page displays) and optionally persist "
            "it to .env."
        )
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("NORTH_BASE_URL"),
        help=(
            "North API base URL. Defaults to NORTH_BASE_URL from "
            ".env / environment."
        ),
    )
    parser.add_argument(
        "--email",
        default=None,
        help=(
            "Email. Defaults to NORTH_ADMIN_EMAIL when --admin is "
            "set, NORTH_EMAIL otherwise."
        ),
    )
    parser.add_argument(
        "--password",
        default=None,
        help=(
            "Password. Defaults to NORTH_ADMIN_PASSWORD when "
            "--admin is set, NORTH_PASSWORD otherwise."
        ),
    )
    parser.add_argument(
        "--write-env",
        action="store_true",
        help=(
            "Write the token to .env at the repo root as "
            "NORTH_API_TOKEN. Replaces any existing line."
        ),
    )
    parser.add_argument(
        "--env-file",
        default=str(repo_root / ".env"),
        help="Path to the .env file (default: <repo_root>/.env).",
    )
    parser.add_argument(
        "--decode",
        action="store_true",
        help=(
            "Print the token's decoded JWT claims (issuer, subject, "
            "email, iat/exp, etc.) to stderr for debugging."
        ),
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help=(
            "Don't print the token to stdout. Use with --write-env "
            "if you only want the side effect."
        ),
    )
    parser.add_argument(
        "--admin",
        action="store_true",
        help=(
            "Use the /admin/ Dex OIDC flow instead of /v1/signin. "
            "Required for accounts that exist only in Dex's local "
            "connector (typical for bootstrap admin users like "
            "admin@cohere.com on internal Cohere tenants)."
        ),
    )
    parser.add_argument(
        "--admin-connector-id",
        default="local",
        help=(
            "Dex connector to use for --admin. Default 'local' "
            "(email+password). Other values like 'google' redirect "
            "to an external IdP and can't be driven with --password."
        ),
    )
    args = parser.parse_args()

    if not args.base_url:
        sys.exit(
            "No base URL configured. Set NORTH_BASE_URL in .env "
            "or pass --base-url. .env is the single source of truth."
        )

    # Pick the right credential pair based on which flow we're using.
    # CLI flags always win; otherwise fall back to the role-specific
    # env var, then to the generic NORTH_EMAIL/NORTH_PASSWORD pair so
    # users with only one identity don't have to duplicate vars.
    if args.admin:
        email = (
            args.email
            or os.environ.get("NORTH_ADMIN_EMAIL")
            or os.environ.get("NORTH_EMAIL")
        )
        password = (
            args.password
            or os.environ.get("NORTH_ADMIN_PASSWORD")
            or os.environ.get("NORTH_PASSWORD")
        )
        missing_hint = "NORTH_ADMIN_EMAIL / NORTH_ADMIN_PASSWORD"
    else:
        email = args.email or os.environ.get("NORTH_EMAIL")
        password = args.password or os.environ.get("NORTH_PASSWORD")
        missing_hint = "NORTH_EMAIL / NORTH_PASSWORD"

    if not email or not password:
        sys.exit(
            f"Need --email and --password (or set {missing_hint} "
            "env vars / .env)."
        )

    if args.admin:
        token = signin_admin_oidc(
            base_url=args.base_url,
            email=email,
            password=password,
            connector_id=args.admin_connector_id,
        )
    else:
        token = signin(
            base_url=args.base_url,
            email=email,
            password=password,
        )

    if args.decode:
        claims = decode_jwt(token) or {}
        iat = claims.get("iat")
        exp = claims.get("exp")
        ttl_h = (
            round((exp - iat) / 3600, 1)
            if isinstance(iat, (int, float))
            and isinstance(exp, (int, float))
            else None
        )
        remaining_h = (
            round((exp - time.time()) / 3600, 1)
            if isinstance(exp, (int, float))
            else None
        )
        when = (
            datetime.fromtimestamp(exp, timezone.utc).isoformat()
            if isinstance(exp, (int, float))
            else "?"
        )
        print(
            f"# JWT claims: sub={claims.get('sub')} "
            f"email={(claims.get('context') or {}).get('email')} "
            f"iss={claims.get('iss')} scope={claims.get('scope')}",
            file=sys.stderr,
        )
        print(
            f"# Token TTL: {ttl_h}h total, "
            f"{remaining_h}h remaining (expires {when})",
            file=sys.stderr,
        )

    if args.write_env:
        env_path = Path(args.env_file).expanduser().resolve()
        action = update_env_file(env_path, "NORTH_API_TOKEN", token)
        print(f"# {action} NORTH_API_TOKEN in {env_path}", file=sys.stderr)

    if not args.quiet:
        print(token)

    return 0


if __name__ == "__main__":
    sys.exit(main())
