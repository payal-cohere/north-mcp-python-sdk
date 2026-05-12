#!/usr/bin/env bash
#
# provision_mvp_north.sh
#
# End-to-end "MVP North" provisioning: stand up N users on a North
# deployment, each with a web-search agent, a my_drive agent, and an
# imported + published automation -- the minimum surface needed to
# demo the full North stack to a new customer or prospect.
#
# Five steps, one script (each loops over the N users provisioned):
#
#   1. Create the users via /v1/signup. One call to
#      examples/customer_demo_env/north_create_user.py with
#      --emails/--passwords/--first-names/--last-names (the script
#      loops internally and treats EMAIL_ALREADY_EXISTS as a soft
#      success that falls back to /v1/signin -- idempotent re-runs).
#   2. Give each user a "Web Reasoning Researcher" agent (hosted
#      tavily_web_search + reasoning + 3 icebreakers) via
#      examples/customer_demo_env/north_create_agent.py in batch
#      mode. That script signs in as each user, creates the agent,
#      and writes tmp/north_users_<ts>.json -- the credentials drop
#      that --for-email lookups in steps 3 and 4 consume.
#   3. For each user, upload a PDF to their my_drive AND create a
#      separate "Document Analyst" agent scoped to that file (using
#      examples/customer_demo_env/north_my_drive.py +
#      examples/customer_demo_env/north_create_my_drive_agent.py).
#   4. For each user, import a .north-automation.json file and
#      publish it (using
#      examples/customer_demo_env/north_import_automation.py).
#      Each user ends up with their OWN copy of the automation.
#   5. For each user, walk through OAuth consent for each hosted
#      tool in TOOL_AUTH_LIST (default: gmail, google_drive) using
#      examples/customer_demo_env/north_manage_tools.py auth-tool.
#      The script opens one browser tab at a time and pauses for
#      ENTER before the next, so N*K tabs don't open at once. Set
#      TOOL_AUTH_LIST='' to skip this step.
#
# What you get at the end (per user):
#   - 1 account, signed in.
#   - 1 web-search agent owned by that user.
#   - 1 my_drive agent owned by that user, with PDF attached.
#   - 1 imported, published automation owned by that user.
#   - OAuth-authenticated hosted tools (gmail, google_drive by
#     default; whichever you completed consent for in Step 5).
#
# Usage
# -----
#   ./examples/customer_demo_env/provision_mvp_north.sh COUNT [OPTIONS]
#
# Positional:
#   COUNT                   Number of users to provision (required, >= 1).
#
# Options:
#   -f, --first-names   STR Comma-separated first names. If omitted,
#                           generated as User1, User2, ...
#   -l, --last-names    STR Comma-separated last names. If omitted,
#                           all default to "Demo".
#   -h, --help              Show this help and exit.
#
# Emails and passwords are ALWAYS auto-generated -- there is no CLI
# flag to override them:
#
#   email    = <firstname>.<lastname>@<NORTH_SIGNUP_DOMAIN>
#              (lowercase, non-alphanumeric chars stripped from the
#              names so "Mary-Jane O'Connor" -> maryjane.oconnor@...)
#   password = 16-char random string with at least one lowercase,
#              uppercase, digit, and special char (matches the
#              _gen_password() scheme in north_create_user.py).
#
# NORTH_SIGNUP_DOMAIN MUST be set in .env to the signup-allowed
# domain for the current deployment, otherwise /v1/signup returns
# EMAIL_DOMAIN_NOT_ALLOWED. The plaintext passwords are printed in
# the final summary; capture them then or pull them from the
# credentials drop at tmp/north_users_<ts>.json (Step 2's output).
#
# Examples:
#   # 5 users, all auto-generated (User1..User5 Demo, emails too)
#   ./examples/customer_demo_env/provision_mvp_north.sh 5
#
#   # 3 users with custom first names (last names default to Demo,
#   # emails derived as alice.demo@..., bob.demo@..., carol.demo@...)
#   ./examples/customer_demo_env/provision_mvp_north.sh 3 \
#     --first-names 'Alice,Bob,Carol'
#
#   # 2 users with full names (emails: alice.smith@..., bob.jones@...)
#   ./examples/customer_demo_env/provision_mvp_north.sh 2 \
#     --first-names 'Alice,Bob' \
#     --last-names  'Smith,Jones'
#
# Other settings come from env vars (no CLI flags):
#   PDF_PATH        path to a PDF (default: ./BOEING_2022_10K.pdf).
#   AUTOMATION_PATH path to a .north-automation.json (default:
#                   ./company-market-research-north-automation.json).
#   WEB_AGENT_NAME, DRIVE_AGENT_NAME  per-agent display names.
#   TOOL_AUTH_LIST  space-separated hosted tools to OAuth in Step 5
#                   (default: 'gmail google_drive'). Set to '' to skip.
#
# Deployment-wide settings come from .env at the repo root:
#   NORTH_BASE_URL, NORTH_SIGNUP_DOMAIN, NORTH_ADMIN_EMAIL,
#   NORTH_ADMIN_PASSWORD. See examples/README.md for the template.
# This script sources .env directly (so $NORTH_SIGNUP_DOMAIN is
# visible to bash and drives the default emails).
#
# Pre-flight assumptions
# ----------------------
#   * Repo virtualenv exists at .venv/bin/python (and has httpx).
#   * .env at the repo root is filled in (see examples/README.md).
#   * If a user already exists on the deployment, /v1/signup returns
#     400 EMAIL_ALREADY_EXISTS and the underlying Python helper
#     falls back to /v1/signin -- but ONLY if the password still
#     matches. With auto-generated passwords every run, re-runs
#     against already-provisioned users will fail signin. Use
#     --emails with stable addresses + delete via admin UI for
#     idempotent re-runs, OR re-use the credentials drop directly.
#   * macOS: PDF_PATH is NOT under ~/Downloads / ~/Documents /
#     ~/Desktop (those are TCC-protected when launched from Cursor).
#     The default ./BOEING_2022_10K.pdf in the repo root is fine.
#   * AUTOMATION_PATH points at a valid .north-automation.json. The
#     default ./company-market-research-north-automation.json is
#     committed to the repo root. Re-running the script imports a
#     *new* automation for each user (no upsert), so duplicates
#     accumulate -- clean up via the UI or by id with
#     curl DELETE /internal/v1/automations/{id} if needed.

set -euo pipefail

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

banner() {
  local title="$1"
  printf '\n%s\n' '============================================================'
  printf '  %s\n' "$title"
  printf '%s\n' '============================================================'
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

usage() {
  sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
}

# Minimal .env loader (bash flavor) so this script can read
# NORTH_SIGNUP_DOMAIN, NORTH_BASE_URL, etc. directly -- matches the
# Python helpers' loader semantics: key=value pairs, # comments and
# blank lines skipped, surrounding "..." or '...' stripped, optional
# `export` prefix tolerated, and any var already in the environment
# wins (so inline `KEY=val ./provision_mvp_north.sh` overrides .env).
load_dotenv() {
  local env_file="$1"
  [[ -f "$env_file" ]] || return 0
  local raw line key val
  while IFS= read -r raw || [[ -n "$raw" ]]; do
    line="${raw#"${raw%%[![:space:]]*}"}"
    [[ -z "$line" || "${line:0:1}" == "#" ]] && continue
    line="${line#export }"
    [[ "$line" != *=* ]] && continue
    key="${line%%=*}"
    val="${line#*=}"
    key="${key%%[[:space:]]*}"
    if [[ "${val:0:1}" == '"' && "${val: -1}" == '"' ]]; then
      val="${val:1:${#val}-2}"
    elif [[ "${val:0:1}" == "'" && "${val: -1}" == "'" ]]; then
      val="${val:1:${#val}-2}"
    fi
    [[ -z "${!key:-}" ]] && export "$key=$val"
  done < "$env_file"
}

# Generate a single 16-char password matching North's signup
# validation (>=1 lowercase, >=1 uppercase, >=1 digit, >=1 special).
# Delegates to Python's `secrets` for CSPRNG bytes -- matches
# _gen_password() in north_create_user.py so generated passwords
# are interchangeable with the Python helper's own output.
generate_password() {
  "$PYTHON" - <<'PYEOF'
import secrets
chars = (
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    "!@#$%^&*"
)
specials = "!@#$%^&*"
while True:
    pw = "".join(secrets.choice(chars) for _ in range(16))
    if (
        any(c.islower() for c in pw)
        and any(c.isupper() for c in pw)
        and any(c.isdigit() for c in pw)
        and any(c in specials for c in pw)
    ):
        print(pw)
        break
PYEOF
}

# Split a comma-separated string into named array elements, trimming
# whitespace around each token. Usage: csv_into 'a, b,c' OUT  ->
# OUT=( "a" "b" "c" ). Uses an indirect set via printf -v to stay
# compatible with bash 3.2 (no `declare -n` nameref needed).
csv_into() {
  local input="$1"
  local arr_name="$2"
  local -a parts
  IFS=',' read -r -a parts <<<"$input"
  local i token
  # Clear the target array first.
  eval "$arr_name=()"
  for i in "${!parts[@]}"; do
    token="${parts[$i]}"
    # ltrim/rtrim whitespace.
    token="${token#"${token%%[![:space:]]*}"}"
    token="${token%"${token##*[![:space:]]}"}"
    eval "$arr_name+=( \"\$token\" )"
  done
}

# Derive an email address from a first + last name + domain. Format:
#   <firstname>.<lastname>@<domain>  (when both names are present)
#   <firstname>@<domain>             (when last is empty)
# Names are lowercased and stripped of any non-alphanumeric chars
# (spaces, hyphens, apostrophes, accents) so the local part is RFC-
# safe. e.g. ("Mary-Jane", "O'Connor", "x.com") -> maryjane.oconnor@x.com.
email_from_name() {
  local first="$1"
  local last="$2"
  local domain="$3"
  local fs ls
  fs="$(printf '%s' "$first" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9')"
  ls="$(printf '%s' "$last"  | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9')"
  if [[ -n "$ls" ]]; then
    printf '%s.%s@%s' "$fs" "$ls" "$domain"
  else
    printf '%s@%s' "$fs" "$domain"
  fi
}

# ---------------------------------------------------------------------------
# Resolve repo root and load .env (so $NORTH_SIGNUP_DOMAIN is set
# before we use it as the default email domain below).
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

[[ -f .env ]] || die "Missing .env at repo root. See examples/README.md for the template."
load_dotenv "$REPO_ROOT/.env"

[[ -n "${NORTH_SIGNUP_DOMAIN:-}" ]] || die "NORTH_SIGNUP_DOMAIN is not set in .env. Set it to the email domain this deployment allow-lists for signup."

PYTHON="$REPO_ROOT/.venv/bin/python"
[[ -x "$PYTHON" ]] || die "Missing $PYTHON. Create the venv first: python -m venv .venv && .venv/bin/pip install -e . httpx"

# ---------------------------------------------------------------------------
# CLI parsing
# ---------------------------------------------------------------------------
COUNT=""
FIRST_NAMES_INPUT=""
LAST_NAMES_INPUT=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    -f|--first-names)
      [[ $# -ge 2 ]] || die "$1 requires a value"
      FIRST_NAMES_INPUT="$2"
      shift 2
      ;;
    -l|--last-names)
      [[ $# -ge 2 ]] || die "$1 requires a value"
      LAST_NAMES_INPUT="$2"
      shift 2
      ;;
    --)
      shift
      break
      ;;
    -*)
      die "unknown option: $1 (use --help)"
      ;;
    *)
      if [[ -z "$COUNT" ]]; then
        COUNT="$1"
      else
        die "unexpected positional argument: $1 (only COUNT is positional)"
      fi
      shift
      ;;
  esac
done

[[ -n "$COUNT" ]] || { usage; die "COUNT is required (number of users to provision)."; }
[[ "$COUNT" =~ ^[0-9]+$ ]] || die "COUNT must be a positive integer (got: '$COUNT')."
[[ "$COUNT" -ge 1 ]] || die "COUNT must be >= 1 (got: $COUNT)."

# ---------------------------------------------------------------------------
# Derive per-user arrays: FIRST_NAMES, LAST_NAMES, then EMAILS, then
# PASSWORDS. Order matters: emails are computed from the name pair
# plus $NORTH_SIGNUP_DOMAIN, so they must come AFTER name resolution.
#
# CLI input (if provided) wins for names; otherwise auto-generate:
#   - FIRST_NAMES default: User1, User2, ...
#   - LAST_NAMES  default: Demo, Demo, ... (same value for all)
# EMAILS and PASSWORDS are ALWAYS auto-generated (no CLI overrides),
# emails from <first>.<last>@$NORTH_SIGNUP_DOMAIN.
# ---------------------------------------------------------------------------
FIRST_NAMES=()
LAST_NAMES=()
EMAILS=()

if [[ -n "$FIRST_NAMES_INPUT" ]]; then
  csv_into "$FIRST_NAMES_INPUT" FIRST_NAMES
  [[ "${#FIRST_NAMES[@]}" -eq "$COUNT" ]] \
    || die "--first-names has ${#FIRST_NAMES[@]} entries but COUNT is $COUNT."
else
  for ((i = 1; i <= COUNT; i++)); do
    FIRST_NAMES+=( "User${i}" )
  done
fi

if [[ -n "$LAST_NAMES_INPUT" ]]; then
  csv_into "$LAST_NAMES_INPUT" LAST_NAMES
  [[ "${#LAST_NAMES[@]}" -eq "$COUNT" ]] \
    || die "--last-names has ${#LAST_NAMES[@]} entries but COUNT is $COUNT."
else
  for ((i = 1; i <= COUNT; i++)); do
    LAST_NAMES+=( "Demo" )
  done
fi

# Build EMAILS as <first>.<last>@$NORTH_SIGNUP_DOMAIN. Detect any
# duplicates (e.g. user passed Alice,Alice as --first-names) and
# fail fast -- /v1/signup would 400 on the second one with
# EMAIL_ALREADY_EXISTS, but failing here gives a clearer message.
for ((i = 0; i < COUNT; i++)); do
  EMAILS+=( "$(email_from_name "${FIRST_NAMES[$i]}" "${LAST_NAMES[$i]}" "$NORTH_SIGNUP_DOMAIN")" )
done
# Duplicate check (collide when names are identical or differ only
# by stripped chars: "Mary-Jane Smith" and "MaryJane Smith" map to
# the same email).
for ((i = 0; i < COUNT; i++)); do
  for ((j = i + 1; j < COUNT; j++)); do
    [[ "${EMAILS[$i]}" != "${EMAILS[$j]}" ]] || die "Derived emails collide: user $((i+1)) and user $((j+1)) both map to ${EMAILS[$i]}. Pass distinct --first-names/--last-names so the local-parts differ."
  done
done

# Auto-generate passwords (unconditionally). 16 chars each, with at
# least one lower, upper, digit, and special. Same scheme as
# north_create_user.py's _gen_password().
PASSWORDS=()
for ((i = 0; i < COUNT; i++)); do
  PASSWORDS+=( "$(generate_password)" )
done

# ---------------------------------------------------------------------------
# Other deployment-level config (env-var only, no CLI flags).
# ---------------------------------------------------------------------------
PDF_PATH="${PDF_PATH:-./BOEING_2022_10K.pdf}"
AUTOMATION_PATH="${AUTOMATION_PATH:-./company-market-research-north-automation.json}"
WEB_AGENT_NAME="${WEB_AGENT_NAME:-Web Reasoning Researcher}"
DRIVE_AGENT_NAME="${DRIVE_AGENT_NAME:-Boeing 10-K Analyst}"
TOOL_AUTH_LIST="${TOOL_AUTH_LIST:-gmail google_drive}"

[[ -r "$PDF_PATH" ]] || die "PDF_PATH not readable: $PDF_PATH (set PDF_PATH=... to override)"
[[ -r "$AUTOMATION_PATH" ]] || die "AUTOMATION_PATH not readable: $AUTOMATION_PATH (set AUTOMATION_PATH=... to override)"

PDF_BASENAME="$(basename "$PDF_PATH")"
AUTOMATION_BASENAME="$(basename "$AUTOMATION_PATH")"

# Print the resolved plan so the user sees exactly what's coming
# before we start hitting the API.
banner "Plan"
echo "  base URL    : ${NORTH_BASE_URL:-<from-helpers>}"
echo "  signup dom. : $NORTH_SIGNUP_DOMAIN"
echo "  users       : $COUNT"
for i in "${!EMAILS[@]}"; do
  printf '    [%d] %-40s  %s %s  (pw: %s)\n' \
      "$((i + 1))" "${EMAILS[$i]}" \
      "${FIRST_NAMES[$i]}" "${LAST_NAMES[$i]}" \
      "${PASSWORDS[$i]}"
done
echo "  pdf         : $PDF_PATH"
echo "  automation  : $AUTOMATION_PATH"
echo "  oauth tools : ${TOOL_AUTH_LIST:-<none, step 5 skipped>}"

# ---------------------------------------------------------------------------
# Step 0: refresh admin JWT into .env (NORTH_API_TOKEN)
# ---------------------------------------------------------------------------
# Most of these calls are user-token calls and don't strictly need
# admin. But we refresh anyway so the .env token is current for any
# follow-up admin commands the user runs (permissions, etc.).
banner "Step 0  Refresh admin JWT (NORTH_API_TOKEN <- .env)"
"$PYTHON" examples/customer_demo_env/north_get_token.py --admin --write-env --quiet
echo "  ok: admin token cached in .env"

# ---------------------------------------------------------------------------
# Step 1: create the users
# ---------------------------------------------------------------------------
# north_create_user.py takes plural --emails/--passwords/--first-names/
# --last-names and loops internally. /v1/signup for each, then /v1/signin
# + /v1/users/me to verify.
#
# Idempotency: north_create_user.py treats EMAIL_ALREADY_EXISTS as a
# soft success -- it falls back to /v1/signin for the existing user
# and records them as "reused" instead of "failed". The bash-level
# `|| ...` below is a belt-and-braces safety net for any *other*
# per-user failure (network blip, password mismatch, etc.): the
# Python script collects failures internally and exits with code 2,
# and we want to keep going to steps 2-5 anyway.
banner "Step 1  Create $COUNT user(s) via /v1/signup"
"$PYTHON" examples/customer_demo_env/north_create_user.py \
    --emails       "${EMAILS[@]}" \
    --passwords    "${PASSWORDS[@]}" \
    --first-names  "${FIRST_NAMES[@]}" \
    --last-names   "${LAST_NAMES[@]}" \
  || echo "  (north_create_user.py reported failures; continuing to step 2 -- see messages above)"

# ---------------------------------------------------------------------------
# Step 2: create a web-search agent for each user
# ---------------------------------------------------------------------------
# north_create_agent.py in batch mode (--emails + --passwords) signs
# in to each existing user, creates one Web Reasoning Researcher
# agent owned by them, and writes tmp/north_users_<ts>.json -- the
# credentials drop that --for-email lookups in steps 3 and 4 read.
#
# Idempotency: agent names are NOT unique server-side, so re-runs
# happily create duplicate agents per user.
banner "Step 2  Create web agent for each of the $COUNT user(s)"
"$PYTHON" examples/customer_demo_env/north_create_agent.py \
    --emails     "${EMAILS[@]}" \
    --passwords  "${PASSWORDS[@]}" \
    --agent-name "$WEB_AGENT_NAME" \
  || echo "  (north_create_agent.py reported failures; continuing to step 3 -- see messages above)"

# Sanity-check the credentials drop. The agent script writes the
# newest file as tmp/north_users_<ts>.json; the my_drive scripts
# pick "the latest" automatically, so we just need to confirm one
# was written.
LATEST_CREDS="$(ls -t tmp/north_users_*.json 2>/dev/null | head -n1 || true)"
[[ -n "$LATEST_CREDS" ]] || die "Web-agent step finished but no tmp/north_users_*.json was written -- aborting."
echo
echo "  credentials drop: $LATEST_CREDS"

# ---------------------------------------------------------------------------
# Step 3: per-user my_drive upload + my_drive agent
# ---------------------------------------------------------------------------
# For each user:
#   a. Upload the PDF. --allow-overwrite makes the upload idempotent
#      if the file is already there from a previous run.
#   b. Create a my_drive agent scoped to just that file. The agent
#      uses /v2/agents (only path that lets us bind specific files).
#      Re-runs create a duplicate agent (no upsert).
banner "Step 3  Upload PDF + my_drive agent for each of the $COUNT user(s) ($PDF_BASENAME)"

for i in "${!EMAILS[@]}"; do
  email="${EMAILS[$i]}"
  printf '\n[user %d/%d] %s\n' "$((i + 1))" "$COUNT" "$email"

  # 3a. Upload
  "$PYTHON" examples/customer_demo_env/north_my_drive.py upload \
      --for-email      "$email" \
      --allow-overwrite \
      "$PDF_PATH" \
    || echo "    (PDF upload failed for $email; continuing)"

  # 3b. Create the my_drive agent. --file-name does a substring
  #     match against the user's drive so PDF renames don't break
  #     the orchestrator. The script also re-fetches metadata
  #     server-side, so the agent's tool config stays consistent.
  "$PYTHON" examples/customer_demo_env/north_create_my_drive_agent.py \
      --for-email "$email" \
      --file-name "$PDF_BASENAME" \
      --name      "$DRIVE_AGENT_NAME" \
    || echo "    (my_drive agent creation failed for $email; continuing)"
done

# ---------------------------------------------------------------------------
# Step 4: per-user automation import + publish
# ---------------------------------------------------------------------------
# Each user gets their OWN copy of the automation -- import takes the
# importer's JWT and assigns the resulting automation to that user.
# We pass --for-email so the orchestrator never touches the admin
# token (which would silently make the automation owned by admin@).
#
# The script chains POST /internal/v1/automations/import (multipart)
# and PUT /internal/v1/automations/{id} with publication_status =
# PUBLISHED. On import, the server creates the automation + its first
# version atomically; the publish step flips it from DRAFT to
# PUBLISHED so it shows up in the user's automations list.
banner "Step 4  Import + publish automation for each of the $COUNT user(s) ($AUTOMATION_BASENAME)"

for i in "${!EMAILS[@]}"; do
  email="${EMAILS[$i]}"
  printf '\n[user %d/%d] %s\n' "$((i + 1))" "$COUNT" "$email"
  "$PYTHON" examples/customer_demo_env/north_import_automation.py \
      --for-email "$email" \
      "$AUTOMATION_PATH" \
    || echo "    (automation import failed for $email; continuing)"
done

# ---------------------------------------------------------------------------
# Step 5: per-user OAuth consent for hosted tools (gmail, drive, ...)
# ---------------------------------------------------------------------------
# Each (user x tool) pair pops ONE browser tab via
# north_manage_tools.py auth-tool --open, then pauses on `read` until
# you've completed Google's consent screen and hit ENTER. This keeps
# COUNT x len(TOOL_AUTHS) OAuth windows from stacking simultaneously.
#
# - `NORTH_API_TOKEN= ...` clears the cached admin JWT for the call,
#   so the script signs in as the target user via /v1/signin instead
#   of silently reusing admin.
# - If a (user, tool) is already authenticated, auth-tool prints
#   "the user is likely already authenticated" and no browser opens.
#   Just press ENTER to move to the next pair.
# - The user can type 's' + ENTER at the prompt to skip ahead anyway.
# - Set TOOL_AUTH_LIST='' (or empty) to skip Step 5 entirely.
if [[ -n "$TOOL_AUTH_LIST" ]]; then
  # shellcheck disable=SC2206  # word-splitting is intentional here
  TOOL_AUTHS=( $TOOL_AUTH_LIST )
  banner "Step 5  OAuth consent: $COUNT user(s) x ${#TOOL_AUTHS[@]} tool(s) = $((COUNT * ${#TOOL_AUTHS[@]})) flow(s)"
  echo "  Tools: ${TOOL_AUTHS[*]}"
  echo "  Each flow opens ONE browser tab; you'll be prompted before the next."
  echo
  for i in "${!EMAILS[@]}"; do
    email="${EMAILS[$i]}"
    password="${PASSWORDS[$i]}"
    for tool in "${TOOL_AUTHS[@]}"; do
      printf '\n---- user %d/%d (%s)  tool: %s ----\n' \
          "$((i + 1))" "$COUNT" "$email" "$tool"
      NORTH_API_TOKEN= "$PYTHON" \
          examples/customer_demo_env/north_manage_tools.py \
          auth-tool "$tool" --open \
          --email "$email" --password "$password" \
        || echo "  (auth-tool exited non-zero; continuing anyway)"
      read -r -p \
        "Press ENTER once consent is complete (or 's' + ENTER to skip ahead): " \
        _resp
      if [[ "${_resp:-}" == "s" || "${_resp:-}" == "S" ]]; then
        echo "  skipped $tool for $email."
      fi
    done
  done
else
  echo
  echo "Skipping Step 5 (TOOL_AUTH_LIST is empty)."
fi

# ---------------------------------------------------------------------------
# Final summary
# ---------------------------------------------------------------------------
banner "Done"
{
  printf 'Provisioned %d user(s), each with:\n' "$COUNT"
  printf '  * 1 web-search agent\n'
  printf '  * 1 my_drive agent (PDF: %s)\n' "$PDF_BASENAME"
  printf '  * 1 imported + published automation (%s)\n' "$AUTOMATION_BASENAME"
  printf '  * OAuth consent walked for: %s\n' "${TOOL_AUTH_LIST:-<none>}"
  printf '\n'
  printf 'Users (capture these passwords NOW -- they were auto-generated):\n'
  for i in "${!EMAILS[@]}"; do
    printf '  %s    [pw: %s]\n' "${EMAILS[$i]}" "${PASSWORDS[$i]}"
  done
  printf '\n'
  printf 'Credentials JSON (used by --for-email in follow-up scripts):\n'
  printf '  %s\n' "$LATEST_CREDS"
  printf '\n'  
}
