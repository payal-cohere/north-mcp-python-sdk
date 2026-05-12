"""
Import a ``.north-automation.json`` file into a North deployment and
(optionally) publish it.

Two-step flow under the hood
----------------------------

1. ``POST /internal/v1/automations/import``
   multipart/form-data, single binary field ``file``.
   The server parses the JSON, validates the graph, and atomically
   creates BOTH the automation record AND its first version.
   Response shape::

       {
         "status": "success" | "error",
         "automation_id": "<uuid>",
         "automation_name": "...",
         "warnings": [...],
         "errors":   [...]
       }

   The new automation is owned by whoever's JWT was used, has
   ``publication_status: DRAFT``, and has ``latest_version`` set.

2. ``PUT  /internal/v1/automations/{automation_id}``
   body ``{"publication_status": "PUBLISHED"}``.
   Flips the new automation from DRAFT to PUBLISHED so other users
   it's shared with can run it. The server rejects publish with
   ``AUTOMATION_NO_VERSION`` if you ever try to publish an
   automation that has no version -- but import guarantees one, so
   that case can't happen on this happy path.

Auth resolution (most ergonomic to most explicit)
-------------------------------------------------
  1. ``--for-email EMAIL``  -- read JWT from latest tmp/north_users_*.json
  2. ``--token JWT``
  3. ``NORTH_API_TOKEN`` env / .env
  4. ``--email`` + ``--password``  (fresh /v1/signin)

Why a fresh user-JWT? The imported automation is **owned by the user
whose JWT was used at import time**. Using the admin token would
create an automation owned by admin@cohere.com. If you want a specific
end-user to own (and see) the automation, use ``--for-email``.

Usage
-----
    # Most common: import + publish for a specific user.
    .venv/bin/python examples/north_import_automation.py \\
        --for-email alice@aadjb2a1f4.com \\
        path/to/insurance-claims.north-automation.json

    # Import only -- leave as DRAFT (e.g. you want to inspect first).
    .venv/bin/python examples/north_import_automation.py --draft \\
        --for-email alice@aadjb2a1f4.com \\
        path/to/insurance-claims.north-automation.json

    # Publish an automation you've already imported (no file needed).
    .venv/bin/python examples/north_import_automation.py \\
        --publish-only \\
        --automation-id ed68dd87-f35a-4510-8442-b97a5f32cb78 \\
        --for-email alice@aadjb2a1f4.com

    # Import for the same user every script reads from .env
    # (NORTH_API_TOKEN). Useful when iterating on your own automation:
    .venv/bin/python examples/north_import_automation.py \\
        path/to/my-wip.north-automation.json
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
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


def _latest_creds_file(repo_root: Path) -> Path | None:
    matches = sorted(glob.glob(str(repo_root / "tmp" / "north_users_*.json")))
    return Path(matches[-1]) if matches else None


def _token_for_email(
    *, repo_root: Path, creds_file: Path | None, email: str
) -> tuple[str, str]:
    path = creds_file or _latest_creds_file(repo_root)
    if not path:
        raise SystemExit(
            "No tmp/north_users_*.json file found. Either pass "
            "--creds-file, run examples/north_create_agent.py "
            "(in batch mode) first, or use --token / --email + "
            "--password."
        )
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise SystemExit(f"Could not read creds file {path}: {e}") from e
    users = data.get("users") or []
    match = next((u for u in users if u.get("email") == email), None)
    if not match:
        emails = sorted(u.get("email") for u in users)
        raise SystemExit(
            f"No user with email {email!r} in {path}. Known: {emails}"
        )
    token = match.get("token")
    if not token:
        raise SystemExit(
            f"User {email!r} in {path} has no cached token. "
            "Re-provision or use --email + --password."
        )
    return token, data.get("base_url") or ""


def _resolve_auth(
    args: argparse.Namespace, *, repo_root: Path
) -> tuple[str, str]:
    if args.for_email:
        creds_path = (
            Path(args.creds_file) if args.creds_file else None
        )
        token, base_from_file = _token_for_email(
            repo_root=repo_root,
            creds_file=creds_path,
            email=args.for_email,
        )
        base = (
            args.base_url
            or base_from_file
            or os.environ.get("NORTH_BASE_URL")
        )
    elif args.token:
        token = args.token
        base = args.base_url or os.environ.get("NORTH_BASE_URL")
    elif args.email and args.password:
        base = args.base_url or os.environ.get("NORTH_BASE_URL")
        if not base:
            raise SystemExit(
                "No base URL configured. Set NORTH_BASE_URL in .env "
                "or pass --base-url."
            )
        token = _signin(
            base_url=base, email=args.email, password=args.password
        )
    else:
        raise SystemExit(
            "Need one of: --for-email <email>  /  --token <jwt>  /  "
            "NORTH_API_TOKEN env var  /  --email + --password."
        )

    if not base:
        raise SystemExit(
            "No base URL configured. Set NORTH_BASE_URL in .env "
            "or pass --base-url."
        )
    return token, base.rstrip("/")


# ---------------------------------------------------------------------------
# API wrappers
# ---------------------------------------------------------------------------


def import_automation(
    *,
    base_url: str,
    token: str,
    file_path: Path,
    timeout: float = 120.0,
) -> dict:
    """POST /internal/v1/automations/import (multipart/form-data).

    Body shape: a single binary field named ``file``. The server
    inspects the JSON inside, runs graph validation, and returns a
    small status envelope (NOT the full Automation):

        {"status": "...", "automation_id": "...",
         "automation_name": "...", "warnings": [...], "errors": [...]}

    Treats any non-empty ``errors`` array as a failure even when the
    HTTP status is 200 -- the server has been observed to 200 a
    partially-valid import while reporting graph problems in
    ``errors``. ``warnings`` are printed but don't fail the call.

    Other failure modes worth knowing about:
      * 500 with PydanticSerializationError -- graph-level validator
        bug (missing required null field, bad edge, unknown
        node_type_id, etc.). Use the version-push trick from the
        north-automation-authoring skill for a clearer error.
      * 422 / 400 -- request-level validation: missing file field,
        not a JSON file, file > server limit.
    """
    if not file_path.is_file():
        raise SystemExit(f"File not found: {file_path}")

    url = f"{base_url.rstrip('/')}/internal/v1/automations/import"
    with file_path.open("rb") as fh:
        r = httpx.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            files={"file": (file_path.name, fh, "application/json")},
            timeout=timeout,
        )
    if r.status_code >= 400:
        raise SystemExit(
            f"Import failed ({r.status_code}) at {url}:\n{r.text}"
        )
    body = r.json() if r.content else {}
    errors = body.get("errors") or []
    if errors:
        raise SystemExit(
            f"Import reported errors:\n"
            + "\n".join(f"  - {e}" for e in errors)
            + f"\n\nfull response: {body}"
        )
    if not body.get("automation_id"):
        raise SystemExit(
            f"Import succeeded but response had no automation_id: {body}"
        )
    return body


def publish_automation(
    *,
    base_url: str,
    token: str,
    automation_id: str,
    timeout: float = 30.0,
) -> dict:
    """PUT /internal/v1/automations/{id} -- flip DRAFT -> PUBLISHED.

    Returns the full updated Automation. Raises SystemExit on the
    common errors:
      * 400 AUTOMATION_NO_VERSION  -- automation has no version yet
        (shouldn't happen after a successful import).
      * 404 AUTOMATION_NOT_FOUND  -- bad id, or the user doesn't
        own/share this automation.
      * 403 -- user lacks can_build_automations.
    """
    url = (
        f"{base_url.rstrip('/')}/internal/v1/automations/{automation_id}"
    )
    r = httpx.put(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={"publication_status": "PUBLISHED"},
        timeout=timeout,
    )
    if r.status_code >= 400:
        raise SystemExit(
            f"Publish failed ({r.status_code}) at {url}:\n{r.text}"
        )
    return r.json()


def get_automation(
    *,
    base_url: str,
    token: str,
    automation_id: str,
    timeout: float = 30.0,
) -> dict:
    url = (
        f"{base_url.rstrip('/')}/internal/v1/automations/{automation_id}"
    )
    r = httpx.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
    )
    if r.status_code >= 400:
        raise SystemExit(
            f"Get automation failed ({r.status_code}): {r.text}"
        )
    return r.json()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_summary(a: dict, *, base_url: str) -> None:
    aid = a.get("id")
    print(f"  id                 : {aid}")
    print(f"  name               : {a.get('name')}")
    print(f"  publication_status : {a.get('publication_status')}")
    print(f"  visibility         : {a.get('visibility')}")
    print(f"  creator            : {a.get('creator_name')}  "
          f"({a.get('creator_id')})")
    lv = a.get("latest_version") or {}
    if isinstance(lv, dict) and lv:
        print(
            f"  latest_version     : id={lv.get('id')}  "
            f"version_number={lv.get('version_number')}"
        )
    elif lv:
        print(f"  latest_version     : {lv}")
    ui_root = base_url.replace("/api", "")
    print(f"  UI                 : {ui_root}/automations/{aid}")


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent.parent
    _load_dotenv(repo_root / ".env")

    parser = argparse.ArgumentParser(
        description=(
            "Import a .north-automation.json file into a North "
            "deployment and (optionally) publish it."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "file",
        nargs="?",
        help=(
            "Path to the .north-automation.json file to import. "
            "Required unless --publish-only is set."
        ),
    )

    # Modes.
    parser.add_argument(
        "--draft",
        action="store_true",
        help=(
            "Import only -- leave the automation as DRAFT. By "
            "default this script imports AND publishes."
        ),
    )
    parser.add_argument(
        "--publish-only",
        action="store_true",
        help=(
            "Skip the import step and just publish an existing "
            "automation. Requires --automation-id."
        ),
    )
    parser.add_argument(
        "--automation-id",
        default=None,
        help=(
            "Automation id to operate on when using --publish-only. "
            "Ignored otherwise (the id is read from the import "
            "response)."
        ),
    )

    # Auth.
    parser.add_argument(
        "--base-url",
        default=os.environ.get("NORTH_BASE_URL"),
    )
    parser.add_argument(
        "--for-email",
        default=None,
        help=(
            "Look up this email in the latest "
            "tmp/north_users_*.json and use that user's cached JWT. "
            "The imported automation will be OWNED by this user."
        ),
    )
    parser.add_argument(
        "--creds-file",
        default=None,
        help=(
            "Override the creds file --for-email reads from. "
            "Default: latest tmp/north_users_*.json."
        ),
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("NORTH_API_TOKEN"),
        help="Explicit JWT. Defaults to NORTH_API_TOKEN env var.",
    )
    parser.add_argument(
        "--email",
        default=os.environ.get("NORTH_EMAIL"),
        help="Email for fresh /v1/signin. Or set NORTH_EMAIL.",
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("NORTH_PASSWORD"),
        help="Password for fresh /v1/signin. Or set NORTH_PASSWORD.",
    )

    args = parser.parse_args()

    if args.publish_only:
        if not args.automation_id:
            parser.error("--publish-only requires --automation-id.")
        if args.file:
            parser.error(
                "--publish-only takes no file argument; "
                "did you mean to drop --publish-only?"
            )
    else:
        if not args.file:
            parser.error("A .north-automation.json path is required.")

    token, base_url = _resolve_auth(args, repo_root=repo_root)

    print("=" * 64)
    print(f"Base URL: {base_url}")
    print(f"Mode    : "
          f"{'publish-only' if args.publish_only else ('import-only' if args.draft else 'import + publish')}")
    print("=" * 64)

    if args.publish_only:
        pre = get_automation(
            base_url=base_url,
            token=token,
            automation_id=args.automation_id,
        )
        print("\nBefore publish:")
        _print_summary(pre, base_url=base_url)
        if pre.get("publication_status") == "PUBLISHED":
            print("\nAlready PUBLISHED; nothing to do.")
            return 0
        after = publish_automation(
            base_url=base_url,
            token=token,
            automation_id=args.automation_id,
        )
        print("\nAfter publish:")
        _print_summary(after, base_url=base_url)
        return 0

    # Import path.
    path = Path(args.file).expanduser().resolve()
    print(f"\nImporting {path} ...")
    imp = import_automation(
        base_url=base_url, token=token, file_path=path
    )
    aid = imp["automation_id"]
    print(f"Imported: {imp.get('automation_name')!r}  id={aid}")
    if imp.get("warnings"):
        print("  warnings:")
        for w in imp["warnings"]:
            print(f"    - {w}")

    # The import response is a thin envelope; fetch the full record
    # so we can show publication_status, latest_version, owner, etc.
    full = get_automation(
        base_url=base_url, token=token, automation_id=aid
    )
    _print_summary(full, base_url=base_url)

    if args.draft:
        print(
            "\n--draft set: leaving automation as DRAFT. To publish "
            f"later, run:\n  examples/north_import_automation.py "
            f"--publish-only --automation-id {aid} "
            "--for-email <user>"
        )
        return 0

    print("\nPublishing ...")
    published = publish_automation(
        base_url=base_url, token=token, automation_id=aid
    )
    print("Published:")
    _print_summary(published, base_url=base_url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
