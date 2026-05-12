"""
Create one Web Reasoning Researcher web-search agent for each of
N **existing** North users.

This is the single, canonical "create web-search agent" script.
Two modes:

  * Batch mode (the common case): pass ``--emails`` + ``--passwords``.
    The script signs in to each user, creates the agent, and writes
    a credentials JSON drop at ``tmp/north_users_<ts>.json``. That
    file is what every downstream script (north_my_drive.py,
    north_create_my_drive_agent.py, north_import_automation.py) reads
    via ``--for-email``.

  * Single-user ad-hoc mode: pass ``--token`` to use a JWT directly.
    The script still creates the agent under that user, but does
    NOT write a credentials drop (it has no password to record).

Why /v1/agents (not /v2/agents)
-------------------------------
The v2 schema exposes a static union of tool types (``web_tools``,
``my_drive``, ``gmail``, ...). But on real North deployments, the
actual built-in hosted tools have *deployment-specific* names like
``tavily_web_search`` -- discoverable via ``GET /internal/v1/tools``.
The legacy /v1/agents endpoint takes a free-form ``north_tool``
wrapper that references those names directly. Posting v2 with
``{"type": "web_tools"}`` to a deployment whose web-search tool
is actually called ``tavily_web_search`` returns 200 but silently
drops the tool. The v1 path is the only one that actually persists.

Per-user flow (batch mode)
--------------------------
  1. POST /v1/signin            -> sign in, get the user's JWT
  2. GET  /v1/users/me          -> resolve user_id
  3. GET  /internal/v1/tools    -> discover hosted tools (cached
                                   across users on the same call)
  4. POST /v1/agents            -> create the agent

Examples
--------
    # Batch: create the web agent for 3 existing users.
    .venv/bin/python examples/north_create_agent.py \\
        --emails    alice@aadjb2a1f4.com bob@aadjb2a1f4.com carol@aadjb2a1f4.com \\
        --passwords 'AlicePass123!' 'BobPass456!' 'CarolPass789!'

    # Override agent metadata for a batch run:
    .venv/bin/python examples/north_create_agent.py \\
        --emails ... --passwords ... \\
        --agent-name "Industry Analyst" \\
        --visibility shared \\
        --instructions-file ./preambles/industry_research.md

    # Single-user with a JWT (e.g. you already have a token):
    .venv/bin/python examples/north_create_agent.py \\
        --token "$JWT" --agent-name "Researcher"

Required env vars
-----------------
  NORTH_BASE_URL    The North API base, e.g. https://host/api
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

import httpx


DEFAULT_MODEL = "north-large-01-2026"
DEFAULT_WEB_SEARCH_TOOL_ID = "tavily_web_search"
DEFAULT_NAME = "Web Reasoning Researcher"
DEFAULT_DESCRIPTION = (
    "Answers complex, multi-step questions by combining live web "
    "search with explicit chain-of-thought reasoning. Always cites "
    "sources and shows its work."
)

DEFAULT_INSTRUCTIONS = """\
You are a research analyst that produces rigorous, source-backed
answers to complex, multi-part questions.

How to think
------------
1. Decompose. Restate the user's question as a numbered list of
   sub-questions before doing anything else. Do not skip this step.
2. Plan tool use. For each sub-question, decide whether you need
   live information from the web. Prefer the web search tool
   whenever the answer depends on facts that may have changed in
   the last 12 months, current prices, recent filings, live news,
   or any quantitative figure.
3. Search broadly, then narrow. Issue at least 2-3 targeted
   queries per sub-question. Compare what multiple reputable
   sources say. If sources disagree, surface the disagreement
   instead of papering over it.
4. Compute explicitly. When the question requires arithmetic
   (ratios, growth rates, real vs. nominal, per-capita, etc.),
   show the formula, the inputs, and the result. Do not just
   assert numbers.

How to answer
-------------
- Lead with a one-paragraph executive summary.
- Then walk through each sub-question in order, showing your
  reasoning and the data you pulled.
- Cite every non-trivial fact with the source URL inline.
- End with a "Caveats / what could change this answer" section.

Style
-----
Concise, technical, no marketing fluff. Bullet points are fine
when they aid scanning. Tables are encouraged for comparisons.
"""

DEFAULT_ICEBREAKERS = [
    (
        "Compare the latest reported quarterly revenue, "
        "year-over-year growth, and revenue per employee for "
        "NVIDIA, AMD, and Intel. Then rank them by capital "
        "efficiency and explain what that ranking implies about "
        "each company's current strategy."
    ),
    (
        "Identify the three countries that issued the largest "
        "sovereign green bond offerings in the last 12 months. "
        "For each, compute the offering size as a percentage of "
        "the country's most recent annual GDP, then rank them by "
        "that ratio to surface the strongest *relative* climate-"
        "finance commitment."
    ),
    (
        "Over the past five years, which has fallen further in "
        "real (inflation-adjusted) terms: median U.S. rent, "
        "median U.S. home price, or the S&P 500's earnings yield? "
        "Show the math using publicly reported data, and argue "
        "which of the three is the best single 'affordability' "
        "indicator for an average U.S. household."
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


def _ts_compact() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


# ---------------------------------------------------------------------------
# API wrappers
# ---------------------------------------------------------------------------


def signin(
    *, base_url: str, email: str, password: str, timeout: float = 30.0
) -> str:
    """POST /v1/signin -- returns the user's JWT."""
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


def whoami(
    *, base_url: str, token: str, timeout: float = 30.0
) -> dict:
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


def list_hosted_tools(
    *, base_url: str, token: str, timeout: float = 30.0
) -> list[dict]:
    """GET /internal/v1/tools -- every built-in hosted tool on the deployment."""
    url = f"{base_url.rstrip('/')}/internal/v1/tools"
    r = httpx.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
    )
    if r.status_code >= 400:
        raise SystemExit(
            f"List tools failed ({r.status_code}) at {url}:\n{r.text}"
        )
    data = r.json()
    if not isinstance(data, list):
        raise SystemExit(f"Unexpected tools response shape: {data!r}")
    return data


def pick_web_search_tool(
    tools: list[dict], preferred_id: str
) -> str:
    """Pick the right hosted-tool name for "web search" on this deployment.

    1. Exact match on ``preferred_id`` against tool ``name``.
    2. Else, any tool whose name contains ``web_search`` / ``websearch``.
    3. Else, any tool whose display/description mentions web search.
    """
    by_name = {
        t.get("name"): t for t in tools if isinstance(t, dict)
    }
    if preferred_id in by_name:
        return preferred_id
    for t in tools:
        if not isinstance(t, dict):
            continue
        tname = (t.get("name") or "").lower()
        if "web_search" in tname or "websearch" in tname:
            return t["name"]
    for t in tools:
        if not isinstance(t, dict):
            continue
        blob = (
            (t.get("display_name") or "")
            + " "
            + (t.get("description") or "")
        ).lower()
        if "search the web" in blob or "web search" in blob:
            return t["name"]
    raise SystemExit(
        "Could not find a web-search hosted tool on this deployment. "
        f"Available tool names: {[t.get('name') for t in tools]}"
    )


def create_agent_v1(
    *,
    base_url: str,
    token: str,
    name: str,
    description: str,
    preamble: str,
    icebreakers: list[str],
    tools: list[dict],
    model: str,
    visibility: str,
    timeout: float = 60.0,
) -> dict:
    """POST /v1/agents -- create one web-search agent for the caller."""
    url = f"{base_url.rstrip('/')}/v1/agents"
    body = {
        "name": name,
        "description": description,
        "preamble": preamble,
        "temperature": 0.3,
        "model": model,
        "visibility": visibility,
        "icebreakers": icebreakers,
        "tools": tools,
        "reasoning_options": {"type": "enabled"},
    }
    r = httpx.post(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=timeout,
    )
    if r.status_code >= 400:
        raise SystemExit(
            f"Create agent failed ({r.status_code}) at {url}:\n{r.text}"
        )
    return r.json()


# ---------------------------------------------------------------------------
# Per-user orchestration
# ---------------------------------------------------------------------------


def _agent_record(agent: dict, *, email: str, password: str,
                  user_id: str | None, token: str) -> dict:
    """Shape one entry of the credentials JSON drop."""
    return {
        "email": email,
        "password": password,
        "user_id": user_id,
        "token": token,
        "agent_id": agent.get("id"),
        "agent_name": agent.get("name"),
        "agent_visibility": agent.get("visibility"),
        "tools": [
            (t.get("north_tool") or {}).get("name")
            for t in (agent.get("tools") or [])
        ],
    }


def add_web_agent_for_user(
    *,
    base_url: str,
    email: str,
    password: str,
    web_search_tool_name: str,
    visibility: str,
    model: str,
    agent_name: str,
    agent_description: str,
    agent_preamble: str,
    agent_icebreakers: list[str],
) -> dict:
    """Sign in + whoami + tool-discovery + create-agent for one user.

    Returns a record matching the credentials-drop schema downstream
    scripts consume via ``--for-email``.
    """
    print(f"\n--- {email} ---")

    print(f"  [1/4] POST /v1/signin")
    token = signin(base_url=base_url, email=email, password=password)

    print(f"  [2/4] GET  /v1/users/me")
    me = whoami(base_url=base_url, token=token)
    user_id = me.get("id")
    print(f"        id={user_id}  email={me.get('email')}")

    print(f"  [3/4] GET  /internal/v1/tools")
    tools = list_hosted_tools(base_url=base_url, token=token)
    web_tool_id = pick_web_search_tool(tools, web_search_tool_name)
    print(f"        web-search tool: {web_tool_id}")

    print(f"  [4/4] POST /v1/agents")
    agent = create_agent_v1(
        base_url=base_url,
        token=token,
        name=agent_name,
        description=agent_description,
        preamble=agent_preamble,
        icebreakers=agent_icebreakers,
        tools=[
            {"type": "north_tool", "north_tool": {"name": web_tool_id}}
        ],
        model=model,
        visibility=visibility,
    )
    print(
        f"        agent_id={agent.get('id')}  "
        f"visibility={visibility}"
    )
    return _agent_record(
        agent,
        email=email,
        password=password,
        user_id=user_id,
        token=token,
    )


def add_web_agent_with_token(
    *,
    base_url: str,
    token: str,
    web_search_tool_name: str,
    visibility: str,
    model: str,
    agent_name: str,
    agent_description: str,
    agent_preamble: str,
    agent_icebreakers: list[str],
) -> dict:
    """Single-user ad-hoc agent creation using a pre-existing JWT.

    No password is known, so the returned record has password=None
    and is NOT written to a credentials JSON drop.
    """
    me = whoami(base_url=base_url, token=token)
    print(
        f"Token resolves to: id={me.get('id')}  "
        f"email={me.get('email')}"
    )
    tools = list_hosted_tools(base_url=base_url, token=token)
    web_tool_id = pick_web_search_tool(tools, web_search_tool_name)
    print(f"Web-search tool: {web_tool_id}")
    agent = create_agent_v1(
        base_url=base_url,
        token=token,
        name=agent_name,
        description=agent_description,
        preamble=agent_preamble,
        icebreakers=agent_icebreakers,
        tools=[
            {"type": "north_tool", "north_tool": {"name": web_tool_id}}
        ],
        model=model,
        visibility=visibility,
    )
    return _agent_record(
        agent,
        email=me.get("email") or "(via --token)",
        password="",
        user_id=me.get("id"),
        token=token,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent.parent
    _load_dotenv(repo_root / ".env")

    parser = argparse.ArgumentParser(
        description=(
            "Create one Web Reasoning Researcher agent for each of "
            "N existing North users (batch mode), or a single agent "
            "via --token. In batch mode, writes a JSON credentials "
            "drop under tmp/ that --for-email lookups consume."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--emails",
        nargs="+",
        metavar="EMAIL",
        default=None,
        help=(
            "Batch mode: one email per user. Required with "
            "--passwords. Mutually exclusive with --token."
        ),
    )
    parser.add_argument(
        "--passwords",
        nargs="+",
        metavar="PASSWORD",
        default=None,
        help=(
            "Batch mode: 1:1 with --emails (same order, same "
            "length). Quote each value with single quotes."
        ),
    )
    parser.add_argument(
        "--token",
        default=None,
        help=(
            "Single-user mode: use this JWT directly. Mutually "
            "exclusive with --emails/--passwords. No credentials "
            "JSON drop is written in this mode."
        ),
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
        "--web-search-tool-id",
        default=DEFAULT_WEB_SEARCH_TOOL_ID,
        help=(
            "Hosted-tool id for web search. Default: "
            f"{DEFAULT_WEB_SEARCH_TOOL_ID}. Falls back to any "
            "tool whose name looks like a web search tool."
        ),
    )
    parser.add_argument(
        "--visibility",
        choices=["private", "public", "shared"],
        default="private",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--agent-name",
        default=DEFAULT_NAME,
        help=f"Agent display name. Default: {DEFAULT_NAME!r}.",
    )
    parser.add_argument(
        "--agent-description", default=DEFAULT_DESCRIPTION
    )
    parser.add_argument(
        "--instructions-file",
        default=None,
        help=(
            "Optional path to a markdown file. If provided, its "
            "contents become the agent preamble; otherwise the "
            "script default is used."
        ),
    )
    parser.add_argument(
        "--output-file",
        default=None,
        help=(
            "Batch mode only: where to write the credentials JSON. "
            "Default: tmp/north_users_<timestamp>.json"
        ),
    )
    args = parser.parse_args()

    # Mode reconciliation. Three valid combinations:
    #   batch    : --emails AND --passwords  (no --token)
    #   single   : --token                   (no --emails/--passwords)
    has_batch = bool(args.emails) or bool(args.passwords)
    has_token = bool(args.token)
    if has_batch and has_token:
        sys.exit(
            "Pass either --emails/--passwords (batch mode) OR "
            "--token (single-user mode), not both."
        )
    if not has_batch and not has_token:
        sys.exit(
            "Need either --emails + --passwords (batch mode) OR "
            "--token (single-user mode)."
        )
    if has_batch:
        if not args.emails or not args.passwords:
            sys.exit(
                "Batch mode requires BOTH --emails and --passwords."
            )
        if len(args.emails) != len(args.passwords):
            sys.exit(
                f"--emails has {len(args.emails)} entries but "
                f"--passwords has {len(args.passwords)}. They must "
                "align 1:1."
            )

    if not args.base_url:
        sys.exit(
            "No base URL configured. Set NORTH_BASE_URL in .env "
            "or pass --base-url."
        )
    base_url = args.base_url.rstrip("/")

    if args.instructions_file:
        preamble = Path(args.instructions_file).read_text()
    else:
        preamble = DEFAULT_INSTRUCTIONS

    # -------- Single-user mode --------
    if has_token:
        print("=" * 72)
        print(f"Base URL    : {base_url}")
        print(f"Mode        : single (--token)")
        print(f"Agent name  : {args.agent_name!r}")
        print("=" * 72)
        rec = add_web_agent_with_token(
            base_url=base_url,
            token=args.token,
            web_search_tool_name=args.web_search_tool_id,
            visibility=args.visibility,
            model=args.model,
            agent_name=args.agent_name,
            agent_description=args.agent_description,
            agent_preamble=preamble,
            agent_icebreakers=DEFAULT_ICEBREAKERS,
        )
        print(f"\nCreated agent {rec['agent_id']!r} "
              f"for user {rec['email']!r}")
        ui_host = base_url[:-4] if base_url.endswith("/api") else base_url
        print(f"UI: {ui_host}/agents/{rec['agent_id']}")
        return 0

    # -------- Batch mode --------
    emails = list(args.emails)
    passwords = list(args.passwords)
    ts = _ts_compact()
    out_path = (
        Path(args.output_file)
        if args.output_file
        else repo_root / "tmp" / f"north_users_{ts}.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print("=" * 72)
    print(f"Base URL    : {base_url}")
    print(f"Mode        : batch ({len(emails)} user(s))")
    print(f"Visibility  : {args.visibility}")
    print(f"Model       : {args.model}")
    print(f"Web tool    : {args.web_search_tool_id}")
    print(f"Agent name  : {args.agent_name!r}")
    print(f"Output file : {out_path}")
    print("=" * 72)

    records: list[dict] = []
    failures: list[dict] = []
    for email, password in zip(emails, passwords):
        try:
            records.append(
                add_web_agent_for_user(
                    base_url=base_url,
                    email=email,
                    password=password,
                    web_search_tool_name=args.web_search_tool_id,
                    visibility=args.visibility,
                    model=args.model,
                    agent_name=args.agent_name,
                    agent_description=args.agent_description,
                    agent_preamble=preamble,
                    agent_icebreakers=DEFAULT_ICEBREAKERS,
                )
            )
        except SystemExit as e:
            print(f"  !! Failed for {email}: {e}")
            failures.append({"email": email, "error": str(e)})
        except Exception as e:  # noqa: BLE001
            print(f"  !! Unexpected error for {email}:")
            traceback.print_exc()
            failures.append({"email": email, "error": repr(e)})

    out_path.write_text(
        json.dumps(
            {
                "base_url": base_url,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "users": records,
                "failures": failures,
            },
            indent=2,
        )
    )
    try:
        out_path.chmod(0o600)
    except OSError:
        pass

    print("\n" + "=" * 72)
    print(f"SUMMARY  ({len(records)} ok, {len(failures)} failed)")
    print("=" * 72)
    print(f"{'#':<3} {'email':<48} {'user_id':<38} {'agent_id'}")
    print("-" * 72)
    for i, r in enumerate(records, 1):
        print(
            f"{i:<3} {r['email']:<48} "
            f"{(r.get('user_id') or '?'):<38} "
            f"{r.get('agent_id') or '?'}"
        )
    if failures:
        print("\nFailures:")
        for f in failures:
            print(f"  - {f['email']}: {f['error'][:200]}")

    print(f"\nCredentials saved to: {out_path}")
    print(
        "(File mode 0600. Contains plaintext passwords + JWTs; "
        "treat like a secret.)"
    )

    return 0 if not failures else 2


if __name__ == "__main__":
    sys.exit(main())
