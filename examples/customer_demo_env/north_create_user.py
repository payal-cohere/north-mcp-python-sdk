"""
Create one or more North users via ``/v1/signup``, sign in as each
to verify the credentials, and print every user's id/JWT/etc.

This is the single, canonical "create users" script. There is no
agent creation, no file upload, no automation work -- those live
in other scripts. By design, this script does NOT write a JSON
credentials drop; that's the job of ``north_create_agent.py`` (in
batch mode), which produces the file the rest of the toolchain
reads via ``--for-email``.

Per-user flow
-------------
For each (email, password, first_name, last_name) tuple:

  1. POST /v1/signup    -> create the user. North returns a JWT.
  2. POST /v1/signin    -> re-auth as the user, prove the password
                            works and grab a fresh JWT.
  3. GET  /v1/users/me  -> confirm identity and capture user_id.

Single-user vs many-user usage
------------------------------
The CLI is "many or one" -- ``--emails`` always takes a list. To
create just one user, pass a single email:

    .venv/bin/python examples/north_create_user.py \\
        --emails alice@aadjb2a1f4.com

When ``--passwords`` is omitted the script auto-generates a strong
random password per user (printed at the end). When ``--first-names``
or ``--last-names`` is omitted, defaults are applied -- ``User<i>``
and ``Demo`` respectively.

When ``--passwords`` is provided it MUST be the same length as
``--emails`` (1:1, same order). The same rule applies to
``--first-names`` and ``--last-names``. Quote each password with
single quotes to prevent shell expansion of ``!``, ``$``, etc.

Examples
--------
    # Create 3 users with everything explicit:
    .venv/bin/python examples/north_create_user.py \\
        --emails    alice@aadjb2a1f4.com bob@aadjb2a1f4.com carol@aadjb2a1f4.com \\
        --passwords 'AlicePass123!' 'BobPass456!' 'CarolPass789!' \\
        --first-names Alice Bob Carol \\
        --last-names  Smith Jones Lee

    # Create 1 user, auto-generated password, default name:
    .venv/bin/python examples/north_create_user.py \\
        --emails alice@aadjb2a1f4.com

Required env vars
-----------------
  NORTH_BASE_URL    The North API base, e.g. https://host/api

  Reads .env at the repo root, but explicit env vars / CLI flags
  always win.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import string
import sys
from pathlib import Path

import httpx


# ---------------------------------------------------------------------------
# Library helpers (also imported by other scripts that need signup/signin)
# ---------------------------------------------------------------------------


def _decode_jwt_payload(token: str) -> dict | None:
    """Best-effort JWT payload decode (no signature verification)."""
    try:
        payload_b64 = token.split(".")[1]
    except IndexError:
        return None
    padding = "=" * (-len(payload_b64) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
    except (ValueError, json.JSONDecodeError):
        return None


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


def _gen_password(length: int = 20) -> str:
    """Generate a strong random password.

    20-char mix of letters/digits/punctuation comfortably clears any
    typical complexity requirement.
    """
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(c.islower() for c in pw)
            and any(c.isupper() for c in pw)
            and any(c.isdigit() for c in pw)
            and any(c in "!@#$%^&*" for c in pw)
        ):
            return pw


class UserAlreadyExists(Exception):
    """Raised by signup() when North returns EMAIL_ALREADY_EXISTS.

    Callers should fall back to /v1/signin to fetch the existing
    user's token (assuming the password still matches). This lets
    re-runs of provisioning scripts be idempotent.
    """

    def __init__(self, email: str, body: dict):
        self.email = email
        self.body = body
        super().__init__(f"user {email!r} already exists")


def signup(
    *,
    base_url: str,
    first_name: str,
    last_name: str | None,
    email: str,
    password: str,
    timeout: float = 30.0,
) -> dict:
    url = f"{base_url.rstrip('/')}/v1/signup"
    body: dict = {
        "first_name": first_name,
        "email": email,
        "password": password,
    }
    if last_name:
        body["last_name"] = last_name
    r = httpx.post(url, json=body, timeout=timeout)
    if r.status_code >= 400:
        # Surface "user already exists" as a typed exception so the
        # caller can fall back to /v1/signin instead of treating it
        # as a hard failure -- the user IS there, we just didn't
        # create them on THIS run.
        try:
            err_body = r.json() or {}
        except (ValueError, json.JSONDecodeError):
            err_body = {}
        if err_body.get("error_code") == "EMAIL_ALREADY_EXISTS":
            raise UserAlreadyExists(email=email, body=err_body)
        raise SystemExit(
            f"Signup failed ({r.status_code}) at {url}:\n{r.text}"
        )
    return r.json()


def signin(
    *,
    base_url: str,
    email: str,
    password: str,
    timeout: float = 30.0,
) -> dict:
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
    return r.json()


def whoami(
    *, base_url: str, token: str, timeout: float = 30.0
) -> dict:
    """GET /v1/users/me -- confirm the token works and fetch the user."""
    url = f"{base_url.rstrip('/')}/v1/users/me"
    r = httpx.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
    )
    if r.status_code >= 400:
        raise SystemExit(
            f"users/me failed ({r.status_code}) at {url}:\n{r.text}"
        )
    return r.json()


# ---------------------------------------------------------------------------
# Per-user orchestration
# ---------------------------------------------------------------------------


def create_one_user(
    *,
    base_url: str,
    email: str,
    password: str,
    first_name: str,
    last_name: str,
) -> dict:
    """Sign up + sign in + whoami for a single user.

    Returns ``{email, password, first_name, last_name, user_id,
    token, already_existed}``. ``already_existed`` is True when
    /v1/signup returned EMAIL_ALREADY_EXISTS and the script fell
    back to /v1/signin instead -- useful for re-running provisioning
    scripts without resetting the deployment.

    Raises SystemExit on any HTTP error OTHER than
    EMAIL_ALREADY_EXISTS. EMAIL_ALREADY_EXISTS combined with a
    matching password is treated as success.
    """
    print(f"\n--- {email} ---")

    signup_token = None
    sub = None
    already_existed = False

    print(f"  [1/3] POST /v1/signup")
    try:
        sresp = signup(
            base_url=base_url,
            first_name=first_name,
            last_name=last_name,
            email=email,
            password=password,
        )
        signup_token = sresp.get("token")
        sub = (
            sresp.get("id")
            or sresp.get("user_id")
            or (
                (_decode_jwt_payload(signup_token) or {}).get("sub")
                if signup_token
                else None
            )
        )
    except UserAlreadyExists:
        # Idempotent path: someone (or a previous run) already
        # signed this email up. Skip the signup_token and let the
        # signin step below produce the JWT. If the stored password
        # no longer matches, /v1/signin will 401 and the batch loop
        # will record this user as a failure with a clear message.
        already_existed = True
        print(
            f"        already exists -- skipping signup, falling "
            f"back to signin to fetch existing user"
        )

    print(f"  [2/3] POST /v1/signin")
    iresp = signin(base_url=base_url, email=email, password=password)
    token = iresp.get("token") or signup_token
    if not token:
        raise SystemExit(f"No JWT returned for {email}.")

    print(f"  [3/3] GET  /v1/users/me")
    me = whoami(base_url=base_url, token=token)
    user_id = me.get("id") or sub
    suffix = "  (already existed)" if already_existed else ""
    print(
        f"        id={user_id}  email={me.get('email')}  "
        f"name={me.get('first_name')} {me.get('last_name')}{suffix}"
    )

    return {
        "email": email,
        "password": password,
        "first_name": first_name,
        "last_name": last_name,
        "user_id": user_id,
        "token": token,
        "already_existed": already_existed,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent.parent
    _load_dotenv(repo_root / ".env")

    parser = argparse.ArgumentParser(
        description=(
            "Create one or more North users via /v1/signup, sign in "
            "as each to verify, and print their credentials."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--emails",
        nargs="+",
        metavar="EMAIL",
        required=True,
        help=(
            "REQUIRED. One email per user. The list length defines "
            "how many users this run creates."
        ),
    )
    parser.add_argument(
        "--passwords",
        nargs="+",
        metavar="PASSWORD",
        default=None,
        help=(
            "Optional. 1:1 with --emails (same order, same length). "
            "If omitted, a strong random password is generated per "
            "user. Quote each value with single quotes."
        ),
    )
    parser.add_argument(
        "--first-names",
        nargs="+",
        metavar="FIRST_NAME",
        default=None,
        help=(
            "Optional. 1:1 with --emails. Default: 'User1', "
            "'User2', ..."
        ),
    )
    parser.add_argument(
        "--last-names",
        nargs="+",
        metavar="LAST_NAME",
        default=None,
        help=(
            "Optional. 1:1 with --emails. Default: 'Demo' for "
            "every user."
        ),
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("NORTH_BASE_URL"),
        help=(
            "North API base URL (must end in /api). Defaults to "
            "NORTH_BASE_URL from .env / environment."
        ),
    )
    args = parser.parse_args()

    if not args.base_url:
        sys.exit(
            "No base URL configured. Set NORTH_BASE_URL in .env "
            "or pass --base-url."
        )

    emails: list[str] = list(args.emails)
    count = len(emails)

    # Validate alignment of every optional 1:1 list.
    for label, lst in (
        ("--passwords", args.passwords),
        ("--first-names", args.first_names),
        ("--last-names", args.last_names),
    ):
        if lst is not None and len(lst) != count:
            sys.exit(
                f"{label} has {len(lst)} entries but --emails has "
                f"{count}. They must align 1:1."
            )

    passwords = (
        list(args.passwords)
        if args.passwords is not None
        else [_gen_password() for _ in range(count)]
    )
    first_names = (
        list(args.first_names)
        if args.first_names is not None
        else [f"User{i + 1}" for i in range(count)]
    )
    last_names = (
        list(args.last_names)
        if args.last_names is not None
        else ["Demo"] * count
    )

    base_url = args.base_url.rstrip("/")
    print("=" * 72)
    print(f"Base URL : {base_url}")
    print(f"Users    : {count}")
    print("=" * 72)

    records: list[dict] = []
    failures: list[dict] = []
    for email, password, fn, ln in zip(
        emails, passwords, first_names, last_names
    ):
        try:
            records.append(
                create_one_user(
                    base_url=base_url,
                    email=email,
                    password=password,
                    first_name=fn,
                    last_name=ln,
                )
            )
        except SystemExit as e:
            print(f"  !! Failed for {email}: {e}")
            failures.append({"email": email, "error": str(e)})

    reused = sum(1 for r in records if r.get("already_existed"))
    created = len(records) - reused
    print("\n" + "=" * 72)
    print(
        f"SUMMARY  ({len(records)} ok = {created} new + {reused} "
        f"already-existing, {len(failures)} failed)"
    )
    print("=" * 72)
    print(f"{'#':<3} {'email':<48} {'user_id':<38} {'state'}")
    print("-" * 72)
    for i, r in enumerate(records, 1):
        state = "reused" if r.get("already_existed") else "new"
        print(
            f"{i:<3} {r['email']:<48} "
            f"{(r.get('user_id') or '?'):<38} {state}"
        )
    if failures:
        print("\nFailures:")
        for f in failures:
            print(f"  - {f['email']}: {f['error'][:200]}")

    print("\nCredentials (save somewhere safe):")
    for r in records:
        print(
            f"  email={r['email']}  password={r['password']}  "
            f"user_id={r['user_id']}"
        )

    return 0 if not failures else 2


if __name__ == "__main__":
    sys.exit(main())
