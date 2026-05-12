"""
Create a North agent with the ``my_drive`` ("My Files") source
enabled and one or more user-uploaded files attached as the
agent's exclusive document context, plus three icebreaker
questions you control.

Why /v2/agents (not /v1/agents)
-------------------------------
On v1 the agent's tools array is a free-form ``north_tool`` reference
with an opaque ``options`` blob. There is *no* documented place to
list specific my_drive files there, and the server bails out with::

    "My drive tool metadata is missing. Only default agents may
     search entire My Drive."

The v2 schema has a typed ``MyDriveTool`` variant whose body
explicitly takes a ``files: [{id, name, type:'file'}]`` array. That's
the canonical "attach these specific files to this agent" mechanism.
After creation, the agent is scoped to *just* those files for any
``my_drive`` lookup.

Endpoint contract
-----------------
``POST /v2/agents`` body::

    {
      "name": "...",
      "description": "...",
      "instructions": "...",
      "icebreakers": ["q1", "q2", "q3"],
      "visibility": "private" | "public" | "shared",
      "reasoning": {"type": "enabled"},
      "tools": [
        {
          "type": "my_drive",
          "files": [
            {"id": "<file_id>", "name": "<file_name>", "type": "file"}
          ]
        }
      ]
    }

Resolving files
---------------
You can pass a file by either:
  * ``--file-id <uuid>``  -- exact id (e.g. from north_my_drive.py list)
  * ``--file-name <name>`` -- substring match against the user's
                             my_drive file_name (case-insensitive)

Both flags are repeatable; the resulting list goes into the agent's
``my_drive.files``. The script always re-fetches the names from the
server at create time so the agent's tool metadata is consistent
even if you only knew the id.

Auth resolution (most ergonomic to most explicit)
-------------------------------------------------
  1. ``--for-email EMAIL``  -- read JWT from latest tmp/north_users_*.json
  2. ``--token JWT``
  3. ``NORTH_API_TOKEN`` env / .env
  4. ``--email`` + ``--password``  (fresh /v1/signin)

Usage
-----
    .venv/bin/python examples/north_create_my_drive_agent.py \\
        --for-email alice@aadjb2a1f4.com \\
        --file-name BOEING_2022_10K.pdf \\
        --name "Boeing 10-K Analyst"

    # Multiple files, custom name + icebreakers:
    .venv/bin/python examples/north_create_my_drive_agent.py \\
        --for-email alice@aadjb2a1f4.com \\
        --file-id 6a5be3ad-1d22-4985-97d8-4b56940d6b40 \\
        --file-id ed9426e0-3333-407c-b6cb-efcfe5a7bdaa \\
        --name "Multi-doc Analyst" \\
        --icebreaker "What are the top 3 risk factors?" \\
        --icebreaker "Summarize the MD&A in 5 bullets." \\
        --icebreaker "What changed in segment revenue YoY?"
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from pathlib import Path

import httpx


DEFAULT_MODEL = "north-large-01-2026"
DEFAULT_AGENT_NAME = "Boeing 10-K Analyst"
DEFAULT_AGENT_DESCRIPTION = (
    "Answers questions about the Boeing 2022 10-K with explicit "
    "citations to specific sections, tables, and pages of the "
    "filing. Refuses to answer from outside knowledge."
)
DEFAULT_AGENT_INSTRUCTIONS = """\
You are a securities-document analyst. Your sole source of truth is the
file(s) attached to your My Files tool.

How to answer
-------------
1. For every factual claim, quote the relevant text from the filing in
   double quotes and include the section heading or page number.
2. If the filing does not contain the answer, say so explicitly. Do NOT
   speculate, do NOT use outside knowledge of the company.
3. When the user asks for a number (revenue, margin, ratio, headcount
   change, etc.), pull the exact figure from the filing's tables, then
   show the arithmetic if any computation is required.
4. When asked to summarize a section, lead with a one-paragraph
   executive summary, then 5-7 bullets of the most material points.
5. When the user asks about risk factors, segment results, or
   contingencies, list them in the order they appear in the filing
   and group them under their original section headings.

Style
-----
Concise. Tables are encouraged for comparisons. Always cite the
section and page. Never repeat boilerplate disclaimers from the filing
unless the user specifically asks about them.
"""

DEFAULT_ICEBREAKERS = [
    (
        "Pull the top five risk factors from Item 1A and rank them by "
        "how much management language emphasizes their severity. Quote "
        "the strongest sentence for each."
    ),
    (
        "Compute Boeing's commercial-airplanes segment revenue change "
        "year-over-year using the figures in the segment-reporting "
        "table. Show the arithmetic and cite the table."
    ),
    (
        "Summarize the MD&A section in seven bullets, in the order the "
        "filing presents them. For each bullet, quote the most "
        "load-bearing sentence and cite its sub-heading."
    ),
]


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
    matches = sorted(
        glob.glob(str(repo_root / "tmp" / "north_users_*.json"))
    )
    return Path(matches[-1]) if matches else None


def _token_for_email(
    *,
    repo_root: Path,
    creds_file: Path | None,
    email: str,
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


def list_my_drive(
    *, base_url: str, token: str, timeout: float = 30.0
) -> list[dict]:
    """GET /internal/v1/my_drive -- returns user's full file index."""
    r = httpx.get(
        f"{base_url}/internal/v1/my_drive",
        headers={"Authorization": f"Bearer {token}"},
        params={"limit": 1000},
        timeout=timeout,
    )
    if r.status_code >= 400:
        raise SystemExit(
            f"List my_drive failed ({r.status_code}): {r.text}"
        )
    return (r.json() or {}).get("files") or []


def resolve_files(
    *,
    base_url: str,
    token: str,
    file_ids: list[str],
    file_names: list[str],
) -> list[dict]:
    """Turn the user's --file-id / --file-name flags into the
    [{id, name, type:'file'}] resources the agent body wants.

    For ``--file-name`` we fetch the user's drive once and match
    case-insensitively as a substring (so "BOEING" matches
    "BOEING_2022_10K.pdf").

    Errors loudly on duplicate / unknown / not-yet-indexed files so
    the caller never silently creates an agent with broken
    attachments.
    """
    drive_files = list_my_drive(base_url=base_url, token=token)
    drive_by_id = {f["file_id"]: f for f in drive_files}

    resolved: list[dict] = []
    seen_ids: set[str] = set()

    for fid in file_ids:
        if fid not in drive_by_id:
            raise SystemExit(
                f"File id {fid!r} not found in user's my_drive. "
                "Use `north_my_drive.py list` to see what's there."
            )
        if fid in seen_ids:
            continue
        seen_ids.add(fid)
        f = drive_by_id[fid]
        resolved.append(
            {"id": fid, "name": f["file_name"], "type": "file"}
        )

    for needle in file_names:
        n = needle.lower()
        matches = [
            f for f in drive_files if n in (f["file_name"] or "").lower()
        ]
        if not matches:
            raise SystemExit(
                f"No file in user's my_drive matches name "
                f"{needle!r}. Available: "
                f"{[f['file_name'] for f in drive_files]}"
            )
        if len(matches) > 1 and not any(
            f["file_name"].lower() == n for f in matches
        ):
            raise SystemExit(
                f"Name {needle!r} matched {len(matches)} files; "
                f"narrow the search or use --file-id. "
                f"Matches: {[f['file_name'] for f in matches]}"
            )
        # Exact match wins over substring matches on duplicates
        chosen = next(
            (f for f in matches if f["file_name"].lower() == n),
            matches[0],
        )
        if chosen["file_id"] in seen_ids:
            continue
        seen_ids.add(chosen["file_id"])
        resolved.append(
            {
                "id": chosen["file_id"],
                "name": chosen["file_name"],
                "type": "file",
            }
        )

    if not resolved:
        raise SystemExit(
            "No files resolved. Pass --file-id and/or --file-name."
        )

    # Surface indexing status as a warning -- attaching an unindexed
    # file is allowed but the agent can't search it until indexing
    # finishes.
    for r in resolved:
        f = drive_by_id[r["id"]]
        if not f.get("is_indexed"):
            print(
                f"  (warn) {f['file_name']!r} is not yet indexed; "
                "the agent will fail to retrieve from it until "
                "indexing completes."
            )
    return resolved


def create_agent_v2(
    *,
    base_url: str,
    token: str,
    name: str,
    description: str,
    instructions: str,
    icebreakers: list[str],
    files: list[dict],
    visibility: str,
    model: str,
    timeout: float = 60.0,
) -> dict:
    """POST /v2/agents with my_drive tool + attached files.

    The v2 typed tool schema is the only path that lets us bind
    specific files to the agent at creation time.
    """
    body = {
        "name": name,
        "description": description,
        "instructions": instructions,
        "model": model,
        "visibility": visibility,
        "reasoning": {"type": "enabled"},
        "icebreakers": icebreakers,
        "tools": [
            {
                "type": "my_drive",
                "files": files,
            }
        ],
    }
    r = httpx.post(
        f"{base_url}/v2/agents",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=timeout,
    )
    if r.status_code >= 400:
        raise SystemExit(
            f"Create agent failed ({r.status_code}) at "
            f"/v2/agents:\n{r.text}"
        )
    return r.json()


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent.parent
    _load_dotenv(repo_root / ".env")

    parser = argparse.ArgumentParser(
        description=(
            "Create a North agent with the my_drive tool and one "
            "or more user files attached as its exclusive context, "
            "plus 3 icebreaker questions."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Auth.
    parser.add_argument(
        "--base-url",
        default=os.environ.get("NORTH_BASE_URL"),
    )
    parser.add_argument("--for-email", default=None)
    parser.add_argument("--creds-file", default=None)
    parser.add_argument(
        "--token", default=os.environ.get("NORTH_API_TOKEN")
    )
    parser.add_argument(
        "--email", default=os.environ.get("NORTH_EMAIL")
    )
    parser.add_argument(
        "--password", default=os.environ.get("NORTH_PASSWORD")
    )

    # File attachment.
    parser.add_argument(
        "--file-id",
        action="append",
        default=[],
        help=(
            "Repeat per file: my_drive file_id to attach. Combine "
            "with --file-name; both contribute to the final list."
        ),
    )
    parser.add_argument(
        "--file-name",
        action="append",
        default=[],
        help=(
            "Repeat per file: case-insensitive substring of the "
            "file name as it appears in the user's my_drive."
        ),
    )

    # Agent metadata.
    parser.add_argument("--name", default=DEFAULT_AGENT_NAME)
    parser.add_argument(
        "--description", default=DEFAULT_AGENT_DESCRIPTION
    )
    parser.add_argument(
        "--instructions-file",
        default=None,
        help=(
            "Path to a markdown/text file. If set, its contents "
            "become the agent's instructions instead of the "
            "default 10-K analyst preamble."
        ),
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--visibility",
        choices=["private", "public", "shared"],
        default="private",
    )
    parser.add_argument(
        "--icebreaker",
        action="append",
        default=[],
        help=(
            "Repeat to provide custom icebreaker questions. If "
            "you don't pass any, the script uses the 3 built-in "
            "10-K analyst defaults."
        ),
    )

    args = parser.parse_args()

    token, base_url = _resolve_auth(args, repo_root=repo_root)

    files = resolve_files(
        base_url=base_url,
        token=token,
        file_ids=args.file_id,
        file_names=args.file_name,
    )
    icebreakers = args.icebreaker or DEFAULT_ICEBREAKERS

    if args.instructions_file:
        instructions = (
            Path(args.instructions_file).expanduser().read_text()
        )
    else:
        instructions = DEFAULT_AGENT_INSTRUCTIONS

    print("=" * 64)
    print(f"Base URL    : {base_url}")
    print(f"Agent name  : {args.name}")
    print(f"Visibility  : {args.visibility}")
    print(f"Model       : {args.model}")
    print(f"Files attached ({len(files)}):")
    for f in files:
        print(f"  - {f['id']}  {f['name']!r}")
    print(f"Icebreakers ({len(icebreakers)}):")
    for q in icebreakers:
        print(f"  - {q[:90]}{'...' if len(q) > 90 else ''}")
    print("=" * 64)

    agent = create_agent_v2(
        base_url=base_url,
        token=token,
        name=args.name,
        description=args.description,
        instructions=instructions,
        icebreakers=icebreakers,
        files=files,
        visibility=args.visibility,
        model=args.model,
    )
    aid = agent.get("id")
    print(f"\nCreated agent {aid}")
    print(
        f"Open in UI: {base_url.replace('/api', '')}/agents/{aid}"
    )
    print("\nServer echo (truncated):")
    keep = {
        "id",
        "name",
        "visibility",
        "model",
        "icebreakers",
        "tools",
    }
    print(
        json.dumps(
            {k: v for k, v in agent.items() if k in keep}, indent=2
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
