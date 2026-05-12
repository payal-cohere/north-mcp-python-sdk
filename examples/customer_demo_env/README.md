# North Customer Demo Environment

A turnkey setup for spinning up a customer-facing demo on a North deployment:
a small set of users, each pre-loaded with the agents, files, automations and
OAuth grants needed to walk a prospect through the platform.

Setup splits into two phases:

| Phase | Owner | File|
|---|---|---|
| **1. Deployment prerequisites** (manual) | Deployment admin (admin UI) | Manual
| **2. Per-user provisioning** (automated) | Every new demo  | [`provision_mvp_north.sh`](./provision_mvp_north.sh) |

If Phase 1 is done correctly, Phase 2 reduces to a single command:

```bash
./examples/customer_demo_env/provision_mvp_north.sh 3
```

---

## Phase 1 — Deployment prerequisites (manual, one-time)

The following must be done **before** running the automation. Most live in the
North admin UI ; a few are owned by the OAuth provider (Google
Cloud Console). None of these are user-specific — they're properties of the
deployment itself.

### 1.1 Admin settings & feature flags

Open `https://<deployment>/admin` and check each item.

| # | Path | Setting | Value | Notes |
|---|---|---|---|---|
| 1 | Configurations → **Automation Settings** | Whether automations are enabled | `true` | Required for the demo's pre-built automation. |
| 2 | Configurations → **Feature flags** | For models that support image input, whether image input is allowed | `true` | 
| 3 | Configurations → **Feature flags** | RAG parsing of docs (image-aware response) | `true` | Image content is taken into account when generating the final response.|
| 4 | Configurations → **Tool catalog** | `web_search` (a.k.a. `tavily_web_search`) | enabled | Should be the **only** default-enabled web tool. The legacy `search_the_web` / `web_scrape` entries should be **disabled** so the catalog is clean. |
| 5 | Configurations → **Tool catalog** | `gmail`, `google_drive` | enabled | 
| 6 | Under Admin → **Permissions** | assign the 'All users' group to **Can use automation**, **Can build automations** |

### 1.2 Google Cloud Console — OAuth app
**TO FIGURE HOW THIS STEP CAN BE AUTOMATED**

For `gmail` and `google_drive` per-user OAuth to succeed in Phase 2, the
deployment's Dex/OAuth callback URL must be registered in the Google Cloud
OAuth client used by the deployment:

1. In the GCP project that owns the deployment's OAuth client, open **APIs &
   Services → Credentials → \<the OAuth 2.0 Client ID>**.
2. Under **Authorized redirect URIs**, ensure the deployment's callback is
   listed:

   ```text
   https://<deployment>.democloud.cohere.com/api/internal/v1/tool/auth
   ```

3. Save. Allow up to a few minutes for Google to propagate the change.

Once this is done, Phase 2 can drive the entire user-consent flow from
the browser — no manual URL fishing required.

### 1.3 Shared demo Gmail / Drive accounts

For consistent demos across customers, the team uses dedicated Google identities
under Cohere's external-demo tenant:

- `misc-north@externaldemo.cohere.com`

### 1.4 `.env` file at the repo root

Create `.env` (`chmod 600`) at the repo root with:

```text
NORTH_BASE_URL=https://<deployment>.democloud.cohere.com/api
NORTH_SIGNUP_DOMAIN=<allow-listed-signup-domain>
NORTH_ADMIN_EMAIL=admin@cohere.com
NORTH_ADMIN_PASSWORD=<from-secured-vault>
NORTH_API_TOKEN=
```

- `NORTH_SIGNUP_DOMAIN` is the email domain the deployment allow-lists for
  `/v1/signup`. Wrong domain → `EMAIL_DOMAIN_NOT_ALLOWED`. Check the deployment's
  tenant config.
- `NORTH_API_TOKEN` is auto-managed; you should leave it blank. Step 0 of the script refreshes it.

### 1.5 install requirements

cd /path-to-repo/north-mcp-python-sdk

#### ---- one-time setup (only run if .venv doesn't exist yet) ----
python3 -m venv .venv
.venv/bin/pip install httpx
#### ---- every new terminal session ----
source .venv/bin/activate

### Phase 1 checklist

- [ ] Automations feature flag = `true`
- [ ] Image input feature flag = `true`
- [ ] RAG image parsing confirmed working
- [ ] `web_search` is the only default-on web tool
- [ ] `gmail`, `google_drive` enabled in the tool catalog
- [ ] `All Users` group granted `Can use automations` + `Can build automations`
- [ ] GCP OAuth client has the deployment's `/internal/v1/tool/auth` redirect URI
- [ ] `.env` filled in at repo root
- [ ] Requirements have been installed

When all boxes are ticked, Phase 2 is unblocked.

---

## Phase 2 — Per-user provisioning (automated)

Everything in this section is driven by [`provision_mvp_north.sh`](./provision_mvp_north.sh).
You can re-run the script as many times as you need; the underlying Python
helpers detect and skip already-existing users.

### What each user gets

Per user, after a successful run:

| # | Resource | Created by | 
|---|---|---|
| 1 | A North account (signup + signin verified) | `north_create_user.py` 
| 2 | A **Web Reasoning Researcher** agent (tavily_web_search + reasoning, 3 icebreakers) | `north_create_agent.py` |
| 3 | A PDF uploaded to the user's My Drive | `north_my_drive.py upload --allow-overwrite` |
| 4 | A **Boeing 10-K Analyst** my_drive agent scoped to that PDF | `north_create_my_drive_agent.py` | 
| 5 | A copy of a pre-built **company-market-research** automation, imported and published | `north_import_automation.py` | 
| 6 | OAuth consent completed for `gmail` and `google_drive` (browser flow) | `north_manage_tools.py auth-tool` | 

### Usage

```bash
./examples/customer_demo_env/provision_mvp_north.sh COUNT [OPTIONS]
```

| Argument | Purpose | Default |
|---|---|---|
| `COUNT` (positional, required) | Number of users to provision, integer ≥ 1 | — |
| `-f, --first-names STR` | Comma-separated first names | `User1, User2, …` |
| `-l, --last-names  STR` | Comma-separated last names | `Demo` for all |
| `-h, --help` | Show inline help | — |

**Emails** and **passwords** are always auto-generated:

- `email = <firstname>.<lastname>@<NORTH_SIGNUP_DOMAIN>` (lowercased, non-alphanumeric chars stripped)
- `password = 16-char random` (≥ 1 lowercase, uppercase, digit, special)

Plaintext passwords are printed in the final summary — capture them then, or
read them from the credentials drop at `tmp/north_users_<ts>.json`.

### Examples

```bash
# Fully auto-generated names + emails (User1.demo, User2.demo, ...)
./examples/customer_demo_env/provision_mvp_north.sh 5

# Custom first names, last names default to "Demo"
./examples/customer_demo_env/provision_mvp_north.sh 3 \
  --first-names 'Alice,Bob,Carol'
# => emails: alice.demo@..., bob.demo@..., carol.demo@...

# Fully customized
./examples/customer_demo_env/provision_mvp_north.sh 2 \
  --first-names 'Alice,Bob' \
  --last-names  'Smith,Jones'
# => emails: alice.smith@..., bob.jones@...
```

### The 5 automated steps

Each invocation runs five steps end-to-end. The first four are silent
(API-only); the fifth is interactive (browser-based OAuth consent).

1. **Step 0 — Refresh admin JWT.** Drives Dex's OIDC code flow as
   `admin@cohere.com` and caches the resulting `NORTH_API_TOKEN` in `.env`.
   Required for any subsequent admin call you may run by hand.
2. **Step 1 — Create users.** Batches `/v1/signup` calls. Treats
   `EMAIL_ALREADY_EXISTS` as a soft success and falls back to `/v1/signin` to
   pick up the existing user's JWT, so re-runs are safe.
3. **Step 2 — Create web-search agents.** One `Web Reasoning Researcher` per
   user. Writes `tmp/north_users_<ts>.json` (the credentials drop consumed by
   later `--for-email` lookups).
4. **Step 3 — Upload PDF + create my_drive agent.** For each user, uploads
   `BOEING_2022_10K.pdf` and creates a `Boeing 10-K Analyst` agent scoped to
   just that file.
5. **Step 4 — Import + publish automation.** For each user, imports
   `company-market-research-north-automation.json` and flips it to `PUBLISHED`
   so it shows up in their **Automations** list.
6. **Step 5 — OAuth consent for hosted tools.** For each (user × tool) pair in
   `TOOL_AUTH_LIST` (default: `gmail google_drive`), the script opens **one**
   browser tab at Google's consent screen, then **pauses** for you to press
   ENTER before moving to the next pair. This deliberately avoids stacking 6+
   browser tabs simultaneously. Type `s` + ENTER at the prompt to skip a pair.

### Environment-variable knobs

| Variable | Default | Purpose |
|---|---|---|
| `PDF_PATH` | `./BOEING_2022_10K.pdf` | PDF uploaded to each user's My Drive in Step 3 |
| `AUTOMATION_PATH` | `./company-market-research-north-automation.json` | Automation imported in Step 4 |
| `WEB_AGENT_NAME` | `Web Reasoning Researcher` | Display name for the Step 2 agent |
| `DRIVE_AGENT_NAME` | `Boeing 10-K Analyst` | Display name for the Step 3 my_drive agent |


---

## File reference

| File | Purpose |
|---|---|
| [`provision_mvp_north.sh`](./provision_mvp_north.sh) | Top-level orchestrator. Runs Steps 0–5 in order. |
| [`north_get_token.py`](./north_get_token.py) | Fetch a JWT via `/v1/signin` (regular) or Dex OIDC (`--admin`). Used by Step 0 to refresh the cached admin token. |
| [`north_create_user.py`](./north_create_user.py) | Batch `/v1/signup` + `/v1/signin` + `/v1/users/me`. Treats `EMAIL_ALREADY_EXISTS` as a soft success. |
| [`north_create_agent.py`](./north_create_agent.py) | Batch-creates a web-search agent for each signed-in user and writes the credentials drop. |
| [`north_my_drive.py`](./north_my_drive.py) | List/upload/delete files in any user's My Drive (uses `--for-email` to pick the user from the credentials drop). |
| [`north_create_my_drive_agent.py`](./north_create_my_drive_agent.py) | Creates a my_drive agent scoped to a specific file already in the user's drive. |
| [`north_import_automation.py`](./north_import_automation.py) | `POST /internal/v1/automations/import` (multipart) + publish. |
| [`north_manage_tools.py`](./north_manage_tools.py) | Per-user + admin tool/connector management. `auth-tool <id>` drives the hosted-tool OAuth flow used by Step 5. |

---
