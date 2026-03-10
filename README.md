# byok-tg-runner

AI-powered Telegram bot running on GitHub Actions with Azure AI Foundry BYOK (Bring Your Own Key) and GitHub Copilot SDK.

Send messages to Telegram — get AI responses powered by your own Azure AI deployment. Create GitHub repos, implement issues, review PRs, and research topics, all from a chat interface.

## Architecture

```
Telegram User
     │
     ▼
Telegram Bot API
     │  webhook
     ▼
┌──────────────────────┐
│  Cloudflare Worker    │  Entrypoint: webhook handler + API gateway
│  (worker/src/index.ts)│  Chat history in CF KV
│  - chat_id whitelist  │  Routes: /task, /trigger, /implement, /task-sync
│  - dual runner health │  Callback: /api/callback (stores bot replies)
└──────────┬───────────┘
           │ health check → pick active runner
     ┌─────┴─────┐
     ▼           ▼
 Runner A    Runner B        GitHub Actions (5.5h loop each)
 (runner-a)  (runner-b)      Mutual monitoring, staggered schedule
     └─────┬─────┘
           ▼
┌──────────────────────┐
│  FastAPI Server       │  server/main.py (uvicorn, port 8000)
│  - POST /task         │  Telegram message → AI → reply
│  - POST /implement    │  Clone repo → AI with shell access → push
│  - POST /trigger      │  External prompt → AI → callback workflow
│  - POST /task-sync    │  /build, /msg (synchronous)
│  - GET  /health       │  Liveness probe
│  - GET  /status       │  Uptime + active tasks
│  - GET  /debug        │  Tool loading + error info
└──────────┬───────────┘
           │
           ▼
┌──────────────────────┐
│  Copilot SDK          │  In-process Python client
│  (github-copilot-sdk) │  BYOK → Azure AI Foundry
│  - Model: gpt-5.2     │  Provider: OpenAI-compatible
│  - Custom @define_tool │  PermissionHandler.approve_all
└──────────┬───────────┘
     ┌─────┴─────────────────┐
     ▼                       ▼
 Telegram reply         GitHub operations
 (httpx POST)           (gh CLI: repos, issues, PRs, workflows)
```

## Public / Private Repo Split

| | Public repo (`byok-tg-runner`) | Private repo (`byok-tg-main`) |
|---|---|---|
| Contains | Infrastructure, server code, worker, workflows | System prompts, custom tools, skills, MCP config |
| Sensitive data | None | Conversation logs (as issues) |
| Role | Execution engine | Configuration & secrets |

The runner pulls private configs at startup:
```
byok-tg-main/prompts/system.md  →  prompt.md
byok-tg-main/tools/*            →  server/
byok-tg-main/skills/            →  server/skills/
byok-tg-main/mcp-config.json    →  ~/.copilot/mcp-config.json
```

## Project Structure

```
byok-tg-runner/
├── .github/workflows/
│   ├── runner-a.yml              # Runner A: 5.5h loop + monitor
│   └── runner-b.yml              # Runner B: 5.5h loop + monitor
├── server/
│   ├── main.py                   # FastAPI + Copilot SDK BYOK
│   ├── tools.py                  # Custom AI tools (create_repo, etc.)
│   ├── shell_tools.py            # Shell/file tools for /implement
│   ├── app_factory.py            # App Factory helpers (repo, issues, pages)
│   └── requirements.txt
├── worker/
│   ├── src/index.ts              # Cloudflare Worker
│   ├── wrangler.toml             # Worker config + KV binding
│   └── package.json
├── templates/workflows/
│   ├── implement.yml             # Auto-injected into child repos
│   └── review.yml                # Auto-injected into child repos
├── scripts/
│   ├── setup.sh                  # First-time installation wizard
│   └── sync-secrets.sh           # Sync shared secrets across platforms
├── docs/plans/                   # Design documents
├── .env.example
└── .gitignore
```

## Telegram Commands

| Command | Handled by | Description |
|---------|-----------|-------------|
| `/app <description>` | Runner | Create a new GitHub repo with AI-generated scaffold |
| `/app fork:<owner/repo> <desc>` | Runner | Fork and customize an existing repo |
| `/issue <owner/repo> <desc>` | Runner | Create structured issue on existing repo |
| `/research <topic>` | Runner | Research and synthesize information |
| `/build <owner/repo>` | Worker | Trigger implement.yml on child repo |
| `/msg <owner/repo>#N <text>` | Worker | Post comment on issue + trigger implement |
| `/status` | Worker | Show runner health and active tasks |
| `/reset` | Worker | Clear chat history |
| _(any text)_ | Runner | General conversation |

## API Endpoints

### FastAPI Server (Runner)

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/task` | POST | `x-api-key` | Process Telegram message (async) |
| `/implement` | POST | `x-api-key` | Clone repo, AI implement/fix/review (async) |
| `/trigger` | POST | `x-api-key` | External AI prompt with workflow callback |
| `/task-sync` | POST | `x-api-key` | Synchronous build/msg operations |
| `/health` | GET | None | Liveness probe: `{"status":"ok"}` |
| `/status` | GET | None | Uptime and active task info |
| `/debug` | GET | None | Tool loading status, errors, active tasks |

### Cloudflare Worker

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/` (POST) | POST | Telegram webhook | Receives and routes Telegram messages |
| `/status` | GET | None | Aggregated status from both runners |
| `/trigger` | POST | `x-api-key` | Proxy to active runner |
| `/implement` | POST | `x-api-key` | Proxy to active runner |
| `/task-sync` | POST | `x-api-key` | Proxy to active runner (synchronous) |
| `/api/callback` | POST | `X-Secret` | Runner callback to store bot replies in KV |
| `/api/history/:chatId` | GET | None | Get merged chat history |
| `/api/stats` | GET | None | Message/app/build counters |
| `/api/repos` | GET | None | All managed repo metadata |

## High Availability

Two runners run staggered on GitHub Actions with mutual monitoring:

```
Time  0h        2.5h       5h         7.5h       10h
      ├──────────┤──────────┤──────────┤──────────┤
  A:  ████████████████████████                     ████████████
  B:           ████████████████████████
              ▲                      ▲
              └── A triggers B       └── B triggers A
```

- Each runner runs a 5.5h loop (66 iterations x 5min interval)
- Every 5 minutes, each runner:
  1. Checks local server health — **auto-restarts uvicorn** if crashed
  2. Checks partner status via `gh run list`
  3. After 2.5h with no partner, triggers it via `gh workflow run`
- Cloudflare Worker health-checks both runners, picks first healthy one
- Cloudflared quick tunnel exposes `localhost:8000` to the internet

## /app — App Factory

The `/app` command creates a complete GitHub project:

1. AI evaluates feasibility and picks tech stack (static-first, zero-deps preferred)
2. `create_repo` — new public repo under `aw-apps` org
3. `setup_repo` — push scaffold (README, AGENTS.md, source files, enable GitHub Pages)
4. Workflow templates (`implement.yml`, `review.yml`) auto-injected from `templates/`
5. `create_issues` — 2-5 structured issues with `copilot-task` label
6. `setup_secrets` — set RUNNER_API_KEY, RUNNER_URL, etc. on child repo
7. Reply to Telegram with repo URL, Pages URL, and next step hint

Then use `/build aw-apps/<repo>` to start the automated implement → review → merge → next issue pipeline.

## /implement — Automated Development Pipeline

Child repos trigger the runner via their `implement.yml` workflow:

```
Issue opened (copilot-task label)
  → implement.yml triggers runner /implement
  → Runner clones repo, gives AI full shell access
  → AI reads issue, creates branch, implements, pushes, creates PR
  → Auto-dispatches review
  → Review: AI checks acceptance criteria, runs browser smoke test
  → APPROVE → merge + dispatch next issue
  → REQUEST_CHANGES → fix-pr cycle
```

Shell tools available to AI during /implement:
- `run_command` — execute any shell command (120s timeout, scoped to repo)
- `read_file` / `write_file` — file operations (path-restricted to repo)
- `list_directory` — directory listing

## /trigger — External Integration

Other repos can trigger AI processing via workflow dispatch:

```yaml
# In any repo's workflow:
- run: |
    curl -s -X POST "${{ secrets.RUNNER_URL }}/trigger" \
      -H "Content-Type: application/json" \
      -H "x-api-key: ${{ secrets.RUNNER_API_KEY }}" \
      -d '{
        "prompt": "Analyze: ${{ github.event.issue.title }}",
        "callback_repo": "${{ github.repository }}",
        "callback_workflow": "on-ai-result.yml",
        "context": "issue-${{ github.event.issue.number }}"
      }'
```

The runner processes the prompt and calls back via `gh workflow run` with the result.

## Secrets

### Shared Secrets (must match on both platforms)

| Secret | GitHub Actions | Cloudflare Worker | Purpose |
|--------|:-:|:-:|---------|
| `RUNNER_API_KEY` | yes | yes | Worker → Runner auth (`x-api-key` header) |
| `TELEGRAM_BOT_TOKEN` | yes | yes | Send Telegram messages |
| `CALLBACK_TOKEN` | yes | yes | Runner → Worker callback auth (`X-Secret` header) |

> **Important:** Use `./scripts/sync-secrets.sh` to update shared secrets. Never update one side manually — it causes auth failures (401).

### GitHub Actions Only

| Secret | Purpose |
|--------|---------|
| `FOUNDRY_API_KEY` | Azure AI Foundry BYOK API key |
| `GH_PAT` | GitHub PAT (workflow scope) for repo operations |
| `CF_ACCOUNT_ID` | Cloudflare account ID (KV writes) |
| `CF_API_TOKEN` | Cloudflare API token (KV writes) |
| `KV_NAMESPACE_ID` | Cloudflare KV namespace ID |
| `TELEGRAM_CHAT_ID` | Telegram chat ID for notifications |
| `WORKER_URL` | Cloudflare Worker URL (for child repo secrets) |
| `APPS_ORG` | GitHub org for app creation (default: `aw-apps`) |

### Cloudflare Worker Only

| Secret | Purpose |
|--------|---------|
| `ALLOWED_CHAT_ID` | Telegram chat ID whitelist |
| `APPS_ORG` | GitHub org name |

## Setup

### Prerequisites

- [GitHub CLI](https://cli.github.com) (`gh`)
- [Node.js](https://nodejs.org) (for wrangler)
- Python 3.10+
- A [Telegram Bot](https://core.telegram.org/bots#botfather) token
- An [Azure AI Foundry](https://ai.azure.com) deployment with API key

### Quick Start

```bash
git clone https://github.com/yazelin/byok-tg-runner.git
cd byok-tg-runner
cd worker && npm install && cd ..
./scripts/setup.sh
```

The setup wizard will:
1. Check prerequisites
2. Create/connect GitHub repo
3. Set up Cloudflare KV namespace
4. Collect all required secrets
5. Configure GitHub Actions + Cloudflare Worker secrets
6. Deploy the Worker
7. Set Telegram webhook
8. Trigger the first runner

### Manual Secret Sync

If you need to update shared secrets after initial setup:

```bash
# Regenerate and sync RUNNER_API_KEY only
./scripts/sync-secrets.sh --key-only

# Regenerate and sync all shared secrets
./scripts/sync-secrets.sh

# Check if secrets are configured on both sides
./scripts/sync-secrets.sh --check

# Then restart runners to pick up new secrets
gh run list --status in_progress -q '.[].databaseId' | xargs -I{} gh run cancel {}
gh workflow run runner-a.yml && gh workflow run runner-b.yml
```

## Log Handling

- FastAPI server outputs **no conversation content** to stdout (safe for public Actions logs)
- Actions log shows only: task accepted, task completed, tool calls, duration
- Full conversations forwarded to `byok-tg-main` private repo as GitHub issues
- Server log written to `/tmp/server.log` on the runner (visible if server crashes)

## Troubleshooting

### Bot not responding

1. **Check runner status:** Send `/status` to the bot, or visit the Worker `/status` endpoint
2. **Both runners offline:** Go to [Actions](../../actions) — if no runs are `in_progress`, trigger one manually
3. **Runner online but no reply:**
   - Check `/debug` endpoint for tool loading errors
   - Server may have crashed — the monitor loop auto-restarts it every 5 minutes
4. **401 errors:** Shared secrets out of sync — run `./scripts/sync-secrets.sh --key-only` and restart runners

### Common issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| "No runner available" | Both runners offline | Trigger `runner-a.yml` manually |
| "Runner error (401)" | `RUNNER_API_KEY` mismatch | `./scripts/sync-secrets.sh --key-only` + restart |
| Bot replies with "Error: ..." | Copilot SDK / Azure AI Foundry issue | Check private repo logs |
| Tunnel not registering | cloudflared timeout | Runner auto-retries; if persistent, check CF status |
| `/app` timeout | Complex project, many tool calls | Timeout is 15min; simplify the request |

## Development

### Local Development

```bash
# Server
cp .env.example .env  # fill in values
pip install -r server/requirements.txt
uvicorn server.main:app --host 0.0.0.0 --port 8000

# Worker
cd worker
cp .dev.vars.example .dev.vars  # fill in values
npx wrangler dev
```

### Deploy Worker Only

```bash
cd worker && npx wrangler deploy
```

### Restart Runners

```bash
# Cancel all running
gh run list --status in_progress -q '.[].databaseId' | xargs -I{} gh run cancel {}

# Start fresh
gh workflow run runner-a.yml
gh workflow run runner-b.yml
```

## Tech Stack

| Component | Technology |
|-----------|-----------|
| AI Runtime | [GitHub Copilot SDK](https://github.com/github/copilot-sdk) (Python, in-process) |
| AI Provider | Azure AI Foundry (BYOK, OpenAI-compatible, `gpt-5.2`) |
| Bot Gateway | Cloudflare Workers (TypeScript) |
| Chat Storage | Cloudflare KV |
| Server | FastAPI + uvicorn |
| Tunnel | cloudflared (quick tunnel → trycloudflare.com) |
| CI/CD | GitHub Actions (dual-runner HA) |
| Browser Testing | Playwright (Chromium, for PR reviews) |

## License

MIT
