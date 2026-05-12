"""
Upload, list, delete, and inspect files in a North user's "My Files"
(``my_drive``) — the per-user document store that the ``my_drive``
hosted tool reads from when an agent is asked to look at user files.

Endpoints used
--------------
Per-user (Bearer = the user's JWT):

  GET    /internal/v1/my_drive/accepted_file_types  -- types + extensions
  GET    /internal/v1/my_drive                       -- list, quota, sizes
  POST   /internal/v1/my_drive/batch_upload          -- multipart upload
         ?allow_overwrite=false&batch_size=20
         form field: ``files`` (repeat per file, binary)
  POST   /internal/v1/my_drive/batch_delete          -- {file_ids: [...]}
  GET    /internal/v1/my_drive/download/{file_id}    -- raw bytes
  GET    /internal/v1/my_drive/meta/{file_id}        -- one file's metadata

Why /internal and not /v1? On every recent North build, ``my_drive``
is exposed under ``/internal/v1/``. The public ``/v1/files/...`` path
exists but is a separate (older / different-purpose) file store --
not the same as the catalog the ``my_drive`` agent tool reads.

What "upload" does, server-side
-------------------------------
For each file:
  1. Stores the raw bytes under the user's account.
  2. Indexes it (chunks + embeds) so the ``my_drive`` tool can
     retrieve it. Indexing is async-ish: the response includes
     ``is_indexed`` (often ``false`` immediately after upload; it
     flips to ``true`` once the indexing job finishes, usually
     seconds later for typical PDFs).
  3. ``allow_overwrite=false`` (default) makes a duplicate-name
     upload fail with code ``DUPLICATE_FILE_NAME``; pass
     ``--allow-overwrite`` to replace the prior content in place.

Auth resolution order (most ergonomic to most explicit)
-------------------------------------------------------
  1. ``--for-email EMAIL``       Look up that email in the most
                                 recent ``tmp/north_users_*.json``
                                 from north_create_agent.py (batch
                                 mode) and use *that* user's cached
                                 JWT. Best UX when you just
                                 provisioned users via the
                                 orchestrator.
  2. ``--token JWT``             Explicit JWT.
  3. ``NORTH_API_TOKEN`` env     The cached token in .env.
  4. ``--email`` + ``--password`` Fresh /v1/signin.

Subcommands
-----------
  accepted-types                          # what file types this deployment allows
  list                                    # list files + quota for the current user
  upload PATH [PATH ...]                  # batch upload (files OR directories)
  delete FILE_ID [FILE_ID ...]            # batch delete by id
  download FILE_ID --out PATH             # save bytes to disk

Usage examples
--------------
    # Upload two specific files for an existing user:
    .venv/bin/python examples/north_my_drive.py upload \\
        --for-email alice@example.com \\
        report.pdf data.csv

    # Upload every file in a directory (top-level only):
    .venv/bin/python examples/north_my_drive.py upload \\
        --for-email alice@example.com \\
        ./reports

    # Upload every file in a directory tree (any depth):
    .venv/bin/python examples/north_my_drive.py upload \\
        --for-email alice@example.com --recursive \\
        ./research-corpus

    # Mix files and directories in one call:
    .venv/bin/python examples/north_my_drive.py upload \\
        --for-email alice@example.com \\
        ./reports cover-letter.pdf ./decks

    # List that user's files:
    .venv/bin/python examples/north_my_drive.py list \\
        --for-email alice@example.com

    # Show what the deployment accepts (no auth scoping needed):
    .venv/bin/python examples/north_my_drive.py accepted-types

Hidden files (names starting with '.', e.g. .DS_Store, .git/) are
silently skipped during directory expansion.
"""

from __future__ import annotations

import argparse
import glob
import json
import mimetypes
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
    *,
    repo_root: Path,
    creds_file: Path | None,
    email: str,
) -> tuple[str, str]:
    """Return (jwt, base_url) for ``email`` from a creds JSON drop.

    Raises SystemExit with a clear pointer if the file or the email
    can't be found, or if the JSON has the wrong shape.
    """
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
            f"No user with email {email!r} in {path}. "
            f"Known emails: {emails}"
        )
    token = match.get("token")
    if not token:
        raise SystemExit(
            f"User {email!r} in {path} has no cached token. "
            "Re-provision or use --email + --password."
        )
    return token, data.get("base_url") or ""


def _resolve_auth(
    args: argparse.Namespace,
    *,
    repo_root: Path,
) -> tuple[str, str]:
    """Return (jwt, base_url). Documented order: for-email > token > env > signin."""
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


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# API wrappers
# ---------------------------------------------------------------------------


def get_accepted_types(
    *, base_url: str, token: str | None = None, timeout: float = 30.0
) -> dict:
    """Read /internal/v1/my_drive/accepted_file_types.

    The endpoint requires auth on most builds but is content-identical
    across every user (it's a deployment-level config).
    """
    url = f"{base_url}/internal/v1/my_drive/accepted_file_types"
    headers = _auth_headers(token) if token else {}
    r = httpx.get(url, headers=headers, timeout=timeout)
    if r.status_code >= 400:
        raise SystemExit(
            f"Accepted-types failed ({r.status_code}): {r.text}"
        )
    return r.json()


def get_my_drive(
    *,
    base_url: str,
    token: str,
    limit: int = 1000,
    offset: int = 0,
    sort_by: str = "date",
    sort_dir: str = "desc",
    q: str | None = None,
    timeout: float = 30.0,
) -> dict:
    url = f"{base_url}/internal/v1/my_drive"
    params: dict = {
        "limit": limit,
        "offset": offset,
        "sort_by": sort_by,
        "sort_dir": sort_dir,
    }
    if q:
        params["q"] = q
    r = httpx.get(
        url, headers=_auth_headers(token), params=params, timeout=timeout
    )
    if r.status_code >= 400:
        raise SystemExit(
            f"List my_drive failed ({r.status_code}): {r.text}"
        )
    return r.json()


def batch_upload(
    *,
    base_url: str,
    token: str,
    file_paths: list[Path],
    allow_overwrite: bool = False,
    batch_size: int = 20,
    timeout: float = 600.0,
) -> list[dict]:
    """POST /internal/v1/my_drive/batch_upload (multipart/form-data).

    Sends every file as a repeated ``files`` form field. Returns the
    server's per-file outcome list (each item is either a
    ``MyDriveFile`` success record or a ``MyDriveFileError``).
    """
    url = f"{base_url}/internal/v1/my_drive/batch_upload"
    params = {
        "allow_overwrite": str(allow_overwrite).lower(),
        "batch_size": batch_size,
    }
    # httpx accepts a list of (field, (filename, fileobj, mimetype))
    # tuples for repeated multipart fields.
    files_payload = []
    open_handles = []
    try:
        for p in file_paths:
            if not p.is_file():
                raise SystemExit(f"Not a file: {p}")
            mime, _ = mimetypes.guess_type(p.name)
            mime = mime or "application/octet-stream"
            fh = p.open("rb")
            open_handles.append(fh)
            files_payload.append(("files", (p.name, fh, mime)))
        r = httpx.post(
            url,
            params=params,
            headers=_auth_headers(token),
            files=files_payload,
            timeout=timeout,
        )
    finally:
        for fh in open_handles:
            try:
                fh.close()
            except OSError:
                pass

    if r.status_code >= 400:
        raise SystemExit(
            f"Upload failed ({r.status_code}) at {url}:\n{r.text}"
        )
    return r.json()


def batch_delete(
    *,
    base_url: str,
    token: str,
    file_ids: list[str],
    timeout: float = 60.0,
) -> dict:
    url = f"{base_url}/internal/v1/my_drive/batch_delete"
    r = httpx.post(
        url,
        headers={**_auth_headers(token), "Content-Type": "application/json"},
        json={"file_ids": file_ids},
        timeout=timeout,
    )
    if r.status_code >= 400:
        raise SystemExit(
            f"Delete failed ({r.status_code}): {r.text}"
        )
    if r.status_code == 204 or not r.content:
        return {}
    try:
        return r.json()
    except json.JSONDecodeError:
        return {"raw": r.text}


def download_file(
    *,
    base_url: str,
    token: str,
    file_id: str,
    out_path: Path,
    timeout: float = 120.0,
) -> int:
    """Stream-download a file from /internal/v1/my_drive/download/{id}.

    Returns the number of bytes written. The server sets
    ``Content-Disposition`` with the original filename; this script
    ignores that (it writes wherever ``out_path`` says).
    """
    url = f"{base_url}/internal/v1/my_drive/download/{file_id}"
    with httpx.stream(
        "GET", url, headers=_auth_headers(token), timeout=timeout
    ) as r:
        if r.status_code >= 400:
            raise SystemExit(
                f"Download failed ({r.status_code}): {r.read()[:300]!r}"
            )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        n = 0
        with out_path.open("wb") as fh:
            for chunk in r.iter_bytes():
                fh.write(chunk)
                n += len(chunk)
        return n


# ---------------------------------------------------------------------------
# CLI handlers
# ---------------------------------------------------------------------------


def _expand_input_paths(
    inputs: list[str], *, recursive: bool
) -> list[Path]:
    """Resolve a mix of file and directory paths into a flat file list.

    Behavior:
      * A file path is included as-is.
      * A directory path is replaced by the (sorted) list of files
        inside it. ``recursive`` controls top-level vs full-tree.
      * Hidden entries (names starting with '.') are silently
        skipped -- prevents .DS_Store, .git/, .ipynb_checkpoints
        etc. from being uploaded by accident.
      * The relative order of distinct CLI arguments is preserved;
        within each expanded directory, files are sorted by name
        for reproducibility.

    Raises SystemExit on a path that doesn't exist or isn't a file
    or directory (e.g. broken symlink, special device).
    """
    out: list[Path] = []
    for raw in inputs:
        p = Path(raw).expanduser()
        if not p.exists():
            raise SystemExit(f"Path does not exist: {p}")
        if p.is_file():
            out.append(p)
            continue
        if not p.is_dir():
            raise SystemExit(f"Not a file or directory: {p}")
        if recursive:
            kids = sorted(
                f
                for f in p.rglob("*")
                if f.is_file()
                and not any(part.startswith(".") for part in f.relative_to(p).parts)
            )
        else:
            kids = sorted(
                f
                for f in p.iterdir()
                if f.is_file() and not f.name.startswith(".")
            )
        if not kids:
            print(
                f"  (warn) {p} has no uploadable files"
                f"{' (try --recursive for nested dirs)' if not recursive else ''};"
                " skipping."
            )
            continue
        out.extend(kids)
    return out


def _bytes_human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024  # type: ignore[assignment]
    return f"{n:.1f} TB"


def cmd_accepted_types(args: argparse.Namespace) -> int:
    repo_root = Path(__file__).resolve().parent.parent.parent
    # Allow this subcommand to run with no auth flags at all -- if a
    # token is around (.env), we use it; otherwise we still try
    # unauthenticated since the endpoint is deployment-static.
    token = (
        args.token
        or os.environ.get("NORTH_API_TOKEN")
    )
    base = (
        args.base_url
        or os.environ.get("NORTH_BASE_URL")
    )
    if not base:
        raise SystemExit(
            "Need NORTH_BASE_URL in .env or --base-url."
        )
    info = get_accepted_types(base_url=base.rstrip("/"), token=token)
    print(json.dumps(info, indent=2))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    repo_root = Path(__file__).resolve().parent.parent.parent
    token, base = _resolve_auth(args, repo_root=repo_root)
    body = get_my_drive(base_url=base, token=token, q=args.query)
    files = body.get("files") or []
    print(
        f"User has {body.get('total_files', '?')} file(s), "
        f"using {_bytes_human(body.get('total_size', 0))} of "
        f"{_bytes_human(body.get('size_allowed', 0))}."
    )
    if args.query:
        print(
            f"Filtered: {body.get('total_filtered_files', '?')} files "
            f"({_bytes_human(body.get('total_filtered_size', 0))}) "
            f"matching {args.query!r}."
        )
    if not files:
        print("  (no files)")
        return 0
    print(
        f"\n{'file_id':<40} {'name':<40} {'size':>10}  indexed?"
    )
    print("-" * 100)
    for f in files:
        print(
            f"{f.get('file_id',''):<40} "
            f"{(f.get('file_name') or '')[:40]:<40} "
            f"{_bytes_human(f.get('file_size') or 0):>10}  "
            f"{'yes' if f.get('is_indexed') else 'no'}"
        )
    return 0


def cmd_upload(args: argparse.Namespace) -> int:
    repo_root = Path(__file__).resolve().parent.parent.parent
    token, base = _resolve_auth(args, repo_root=repo_root)
    paths = _expand_input_paths(args.files, recursive=args.recursive)
    if not paths:
        raise SystemExit(
            "Nothing to upload after expanding the given paths."
        )
    print(
        f"Uploading {len(paths)} file(s) to {base} "
        f"(allow_overwrite={args.allow_overwrite}, "
        f"recursive={args.recursive})..."
    )
    for p in paths:
        print(f"  - {p}")
    results = batch_upload(
        base_url=base,
        token=token,
        file_paths=paths,
        allow_overwrite=args.allow_overwrite,
        batch_size=args.batch_size,
    )
    ok = [r for r in results if r.get("type") == "file"]
    err = [r for r in results if r.get("type") == "error"]
    print(f"\nUploaded {len(ok)} OK, {len(err)} failed.")
    for r in ok:
        print(
            f"  OK  {r.get('file_id')}  {r.get('file_name')!r}  "
            f"{_bytes_human(r.get('file_size') or 0)}  "
            f"indexed={r.get('is_indexed')}"
        )
    for r in err:
        print(
            f"  ERR {r.get('code')}: {r.get('error')}  "
            f"(file={r.get('file_name')!r})"
        )
    return 0 if not err else 2


def cmd_delete(args: argparse.Namespace) -> int:
    repo_root = Path(__file__).resolve().parent.parent.parent
    token, base = _resolve_auth(args, repo_root=repo_root)
    if not args.yes:
        confirm = input(
            f"Delete {len(args.file_ids)} file(s) from my_drive? "
            "Type 'yes' to confirm: "
        )
        if confirm.strip().lower() != "yes":
            print("Aborted.")
            return 1
    res = batch_delete(
        base_url=base, token=token, file_ids=args.file_ids
    )
    print(f"Deleted {len(args.file_ids)} file(s).")
    if res:
        print(json.dumps(res, indent=2))
    return 0


def cmd_download(args: argparse.Namespace) -> int:
    repo_root = Path(__file__).resolve().parent.parent.parent
    token, base = _resolve_auth(args, repo_root=repo_root)
    out = Path(args.out).expanduser()
    n = download_file(
        base_url=base, token=token, file_id=args.file_id, out_path=out
    )
    print(f"Wrote {_bytes_human(n)} to {out}")
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
        "--for-email",
        default=None,
        help=(
                "Look up this email in the latest "
                "tmp/north_users_*.json (from north_create_agent.py "
                "in batch mode) and use that user's cached JWT. "
                "Most ergonomic option."
        ),
    )
    p.add_argument(
        "--creds-file",
        default=None,
        help=(
            "Override the creds file --for-email looks up. "
            "Default: latest tmp/north_users_*.json."
        ),
    )
    p.add_argument(
        "--token",
        default=os.environ.get("NORTH_API_TOKEN"),
        help=(
            "Explicit JWT. Defaults to NORTH_API_TOKEN env var. "
            "Skipped when --for-email is set."
        ),
    )
    p.add_argument(
        "--email",
        default=os.environ.get("NORTH_EMAIL"),
        help="Email for fresh /v1/signin. Or set NORTH_EMAIL.",
    )
    p.add_argument(
        "--password",
        default=os.environ.get("NORTH_PASSWORD"),
        help="Password for fresh /v1/signin. Or set NORTH_PASSWORD.",
    )


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent.parent
    _load_dotenv(repo_root / ".env")

    parser = argparse.ArgumentParser(
        description=(
            "Manage a user's My Files (my_drive) on a North "
            "deployment: upload, list, delete, download."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_at = sub.add_parser(
        "accepted-types",
        help="Show file types and extensions this deployment accepts.",
    )
    _add_auth_args(p_at)
    p_at.set_defaults(handler=cmd_accepted_types)

    p_list = sub.add_parser(
        "list",
        help="List a user's my_drive files + quota usage.",
    )
    p_list.add_argument(
        "--query", "-q",
        default=None,
        help="Optional substring filter on file names.",
    )
    _add_auth_args(p_list)
    p_list.set_defaults(handler=cmd_list)

    p_up = sub.add_parser(
        "upload",
        help=(
            "Batch-upload files to a user's my_drive. Positional "
            "arguments may be individual files OR directories; "
            "directories are expanded to the files inside them "
            "(top-level only by default, full tree with --recursive). "
            "Hidden files (.DS_Store etc.) are skipped."
        ),
    )
    p_up.add_argument(
        "files",
        nargs="+",
        metavar="PATH",
        help=(
            "One or more file or directory paths. Directories are "
            "expanded to their contained files."
        ),
    )
    p_up.add_argument(
        "--recursive",
        "-r",
        action="store_true",
        help=(
            "When a directory is given, walk it recursively and "
            "upload every file in the tree. Without this flag, "
            "only the directory's top-level files are uploaded."
        ),
    )
    p_up.add_argument(
        "--allow-overwrite",
        action="store_true",
        help=(
            "Replace existing files with the same name instead of "
            "failing with DUPLICATE_FILE_NAME."
        ),
    )
    p_up.add_argument(
        "--batch-size",
        type=int,
        default=20,
        help="Server-side batch size hint. Default: 20.",
    )
    _add_auth_args(p_up)
    p_up.set_defaults(handler=cmd_upload)

    p_del = sub.add_parser(
        "delete",
        help="Batch-delete files by file_id.",
    )
    p_del.add_argument("file_ids", nargs="+")
    p_del.add_argument(
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt.",
    )
    _add_auth_args(p_del)
    p_del.set_defaults(handler=cmd_delete)

    p_dl = sub.add_parser(
        "download",
        help="Download one file by id, write bytes to --out.",
    )
    p_dl.add_argument("file_id")
    p_dl.add_argument(
        "--out",
        required=True,
        help="Output path on local disk.",
    )
    _add_auth_args(p_dl)
    p_dl.set_defaults(handler=cmd_download)

    args = parser.parse_args()
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
