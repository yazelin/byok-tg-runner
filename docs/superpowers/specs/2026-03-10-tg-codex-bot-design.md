# tg-codex-bot Design Spec

## Overview

Merge byok-tg-main + byok-tg-runner into a single private repo (`tg-codex-bot`) that uses GitHub Codespace devcontainer to run OpenAI Codex CLI as the AI agent, with Cloudflare Worker handling simple tasks directly.

## Goals

- Single private repo (no public/private split)
- Simple chat → CF Worker + OpenAI API (millisecond response)
- Complex tasks → Codespace + Codex CLI full-auto (seconds)
- Codespace start on demand, stop immediately after task completion
- One-click `setup.sh` installation
- Webhook secret verification for Telegram

## Architecture

```
Telegram User
     │ webhook (X-Telegram-Bot-Api-Secret-Token verification)
     ▼
┌─────────────────────────────────────────────────┐
│  Cloudflare Worker (TypeScript)                  │
│                                                  │
│  FAST PATH (Worker handles directly):            │
│  ├─ /reset           → clear KV memory           │
│  ├─ /status          → Codespace status           │
│  ├─ /build <repo>    → gh workflow dispatch       │
│  ├─ /msg <repo>#N    → gh issue comment + trigger │
│  ├─ /setpref key val → write KV preferences       │
│  └─ simple chat      → OpenAI API (gpt-5.3-codex) │
│     └─ if <<<ROUTE_TO_CODEX>>> → upgrade to Codex │
│                                                  │
│  CODEX PATH (Codespace):                         │
│  ├─ /app [fork:repo] desc  → create project      │
│  ├─ /issue repo desc       → structured issue     │
│  ├─ /research topic        → research + synthesize │
│  ├─ /implement             → AI code implementation│
│  └─ upgraded chat          → complex reasoning     │
│                                                  │
│  Codex path flow:                                │
│  1. Reply Telegram "處理中..."                    │
│  2. GitHub API: check Codespace state             │
│  3. If Stopped → start (10-15s)                   │
│  4. If not exist → create (30-60s)                │
│  5. Get port forwarding URL                       │
│  6. POST task to Codespace task server             │
│  7. Wait for Codex CLI result                      │
│  8. Reply Telegram with result                     │
│  9. Store KV history + log as Issue                │
│  10. GitHub API: stop Codespace                    │
└─────────────────────────────────────────────────┘
         │                         │
         ▼                         ▼
   OpenAI API                GitHub Codespace (Ubuntu)
   (simple chat)             ┌──────────────────────┐
                             │ devcontainer          │
                             │ ├─ task server :8080  │
                             │ ├─ codex CLI          │
                             │ │  -q -a full-auto    │
                             │ │  (no sandbox on     │
                             │ │   Linux, network OK)│
                             │ ├─ gh CLI             │
                             │ └─ git                │
                             └──────────────────────┘
```

## Repo Structure

```
tg-codex-bot/                            (private repo)
├── .devcontainer/
│   ├── devcontainer.json                Codespace config
│   └── post-start.sh                   Auto-start task server
├── worker/
│   ├── src/index.ts                     CF Worker: webhook, routing, OpenAI chat
│   ├── wrangler.toml                    KV binding + env vars
│   └── package.json
├── server/
│   ├── main.py                          Task server (runs in Codespace)
│   ├── codex_runner.py                  Codex CLI wrapper (subprocess)
│   ├── app_factory.py                   Repo creation, setup, issues, secrets
│   └── requirements.txt
├── prompts/
│   └── system.md                        System prompt (chat + app factory rules)
├── templates/
│   └── workflows/
│       ├── implement.yml                Injected into child repos
│       └── review.yml                   Injected into child repos
├── config/
│   └── codex-config.yaml                Codex CLI config (model, approvalMode)
├── scripts/
│   ├── setup.sh                         One-click installation
│   └── sync-secrets.sh                  Sync shared secrets
├── AGENTS.md                            Project conventions for Codex
└── README.md
```

## Components

### 1. Cloudflare Worker

**Responsibilities:**
- Telegram webhook receiver with secret verification
- Chat history management (KV)
- User preferences management (KV)
- Stats tracking (KV)
- Simple chat via OpenAI API
- Smart routing: detect complex requests → upgrade to Codex
- Codespace lifecycle management via GitHub API
- API endpoints: /api/history, /api/stats, /api/repos, /api/callback

**Simple chat flow:**
- Worker calls OpenAI API directly with system prompt + history
- System prompt instructs model to return `<<<ROUTE_TO_CODEX>>>` if task requires shell access, repo operations, or multi-step reasoning
- If upgrade marker detected → switch to Codex path

**Codespace management flow:**
1. `GET /user/codespaces` → find codespace for this repo
2. If state=Stopped → `POST /user/codespaces/{name}/start`
3. Poll `GET /user/codespaces/{name}` until state=Available
4. Get forwarded port URL from codespace ports API
5. POST task to `{port_url}/task`
6. After result received → `POST /user/codespaces/{name}/stop`

**KV storage:**
- `chat:{chatId}:user` / `chat:{chatId}:bot` — history (max 20 per role)
- `chat:{chatId}:prefs` — user preferences (language, MVP preference, etc.)
- `stats:totalMessages` / `stats:totalApps` / `stats:totalBuilds` — counters
- `repo:{name}` — repo metadata

### 2. Task Server (Codespace)

**Runs inside Codespace, started by postStartCommand.**

**Endpoints:**
- `GET /health` — liveness probe
- `POST /task` — execute Codex CLI task

**POST /task request:**
```json
{
  "prompt": "user message with history context",
  "command": "app|issue|research|implement|chat",
  "chat_id": "telegram chat id",
  "repo": "owner/repo (for implement)",
  "action": "implement|fix-pr|review (for implement)",
  "issue_number": 0,
  "pr_number": 0
}
```

**POST /task response:**
```json
{
  "status": "ok",
  "reply": "AI response text",
  "tools_used": ["create_repo", "setup_repo"]
}
```

**Task processing:**
- Build full prompt from: system prompt + history + command-specific instructions
- For /app, /issue, /research, chat: run `codex -q -a full-auto "prompt"`
- For /implement: clone repo → run codex with shell access in repo dir
- Capture stdout → parse response → return JSON
- Log conversation as GitHub Issue via `gh issue create`

### 3. Codex CLI Configuration

**~/.codex/config.yaml** (generated from config/codex-config.yaml):
```yaml
model: gpt-5.3-codex
provider: openai
approvalMode: full-auto
fullAutoErrorMode: ignore-and-continue
notify: false
```

**Environment variables (from Codespace Secrets):**
- `OPENAI_API_KEY` — API key for Codex
- `OPENAI_BASE_URL` — custom endpoint (optional)

**Execution:**
```bash
codex -q -a full-auto -m gpt-5.3-codex "prompt"
```

- `-q` — quiet/non-interactive (no TUI)
- `-a full-auto` — auto-approve all operations
- Linux Codespace: no sandbox, full network access (gh, git, curl all work)

### 4. devcontainer.json

```jsonc
{
  "image": "mcr.microsoft.com/devcontainers/universal:2",
  "features": {
    "ghcr.io/devcontainers/features/github-cli:1": {},
    "ghcr.io/devcontainers/features/node:1": {}
  },
  "postCreateCommand": "npm install -g @openai/codex && pip install fastapi uvicorn httpx && mkdir -p ~/.codex && cp config/codex-config.yaml ~/.codex/config.yaml",
  "postStartCommand": ".devcontainer/post-start.sh",
  "forwardPorts": [8080],
  "portsAttributes": {
    "8080": {
      "label": "Task Server",
      "visibility": "public"
    }
  }
}
```

**post-start.sh:**
```bash
#!/bin/bash
cd /workspaces/tg-codex-bot
uvicorn server.main:app --host 0.0.0.0 --port 8080 &
echo "Task server started on :8080"
```

### 5. System Prompt (prompts/system.md)

Used in two contexts:
- **Worker (OpenAI API chat):** Lightweight version for simple conversation
- **Codespace (Codex CLI):** Full version loaded via AGENTS.md

Key directives:
- Respond in Traditional Chinese (繁體中文) by default
- 4096 char limit per message (Telegram)
- Return `<<<ROUTE_TO_CODEX>>>` if task requires: shell access, repo operations, multi-step implementation, web research
- /app: App Factory rules (static > backend, zero deps, MVP vs full based on user pref)
- /issue: Structured issue format (Objective/Context/Approach/Acceptance Criteria)
- /research: Synthesize and summarize

### 6. App Factory

Same workflow as byok-tg-runner, but executed via Codex CLI:

1. Codex evaluates feasibility, picks tech stack
2. Uses `gh` CLI to create repo under configured org
3. Writes scaffold files, pushes to repo
4. Injects workflow templates from templates/workflows/
5. Creates structured issues with copilot-task label
6. Sets child repo secrets
7. Returns summary to Telegram

### 7. Implement Pipeline

Triggered by child repo workflows or `/build` command:

```
Issue (copilot-task label)
  → child repo implement.yml → POST to Worker /implement
  → Worker starts Codespace → POST /task (action=implement)
  → Codex: clone repo, read issue, create branch, implement, push, create PR
  → Codex: auto-dispatch review
  → Review: check acceptance criteria, Playwright smoke test
  → APPROVE → merge → next issue
  → REQUEST_CHANGES → fix-pr cycle
  → After all done → stop Codespace
```

## Secrets

### Codespace Secrets (auto-injected as env vars)

| Secret | Purpose |
|--------|---------|
| `OPENAI_API_KEY` | Codex CLI + OpenAI API |
| `OPENAI_BASE_URL` | Custom API endpoint (optional) |
| `TASK_API_KEY` | Auth for task server endpoints |
| `GH_PAT` | GitHub PAT for repo operations |

### Cloudflare Worker Secrets

| Secret | Purpose |
|--------|---------|
| `OPENAI_API_KEY` | Worker direct OpenAI chat |
| `OPENAI_BASE_URL` | Custom API endpoint |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot API |
| `TELEGRAM_SECRET` | Webhook verification |
| `ALLOWED_CHAT_ID` | Chat whitelist |
| `TASK_API_KEY` | Auth for Codespace task server |
| `GH_PAT` | GitHub API (Codespace management) |

### GitHub Repo Secrets

| Secret | Purpose |
|--------|---------|
| `CF_ACCOUNT_ID` | Cloudflare account (for setup.sh) |
| `CF_API_TOKEN` | Cloudflare API (for setup.sh) |

### Shared Secrets (must match)

| Secret | Where |
|--------|-------|
| `TASK_API_KEY` | Codespace Secret + CF Worker |
| `OPENAI_API_KEY` | Codespace Secret + CF Worker |
| `OPENAI_BASE_URL` | Codespace Secret + CF Worker |

→ Use `scripts/sync-secrets.sh` to keep in sync.

## One-Click Setup (scripts/setup.sh)

```
1.  Check prerequisites: gh, node, npx (wrangler)
2.  Create private repo (or use existing)
3.  Collect secrets:
    - Telegram Bot Token
    - Telegram Chat ID
    - OpenAI API Key
    - OpenAI Base URL (default: https://api.openai.com/v1)
    - GitHub PAT (scopes: codespace, repo, workflow)
    - Cloudflare API Token + Account ID
4.  Auto-generate: TASK_API_KEY, TELEGRAM_SECRET
5.  Set Codespace Secrets (gh codespace secret set)
6.  Set GitHub Repo Secrets (gh secret set)
7.  Set CF Worker Secrets + deploy Worker (wrangler)
8.  Register Telegram webhook with secret
9.  Create Codespace (gh codespace create --repo owner/tg-codex-bot)
10. Start Codespace, verify task server health
11. Stop Codespace
12. Send test message to verify end-to-end
```

## Security

- Telegram webhook: `X-Telegram-Bot-Api-Secret-Token` header verification
- Worker API endpoints: `X-Secret` header (TASK_API_KEY)
- Task server: `X-Api-Key` header (TASK_API_KEY)
- Chat whitelist: `ALLOWED_CHAT_ID` in Worker
- Codespace: private repo, secrets injected as env vars
- Codex full-auto: Linux (no sandbox), but Codespace is already isolated
- Conversation logs: GitHub Issues in same private repo

## Key Differences from Previous Architecture

| Aspect | byok-tg (before) | tg-codex-bot (new) |
|--------|-------------------|---------------------|
| AI runtime | Copilot SDK (Python, in-process) | Codex CLI (Node, subprocess) |
| AI provider | Azure AI Foundry (gpt-5.2) | OpenAI BYOK (gpt-5.3-codex) |
| Execution env | GitHub Actions (5.5h loop) | Codespace (on-demand start/stop) |
| Simple chat | AI handles all | Worker + OpenAI API direct |
| HA strategy | Dual runner + tunnel | Single Codespace (start/stop) |
| Repo split | Public + Private | Single private repo |
| Config | Python env vars | codex-config.yaml + Codespace Secrets |
| Dashboard | API endpoints (no UI) | Not included (private repo) |
| Cost model | Actions minutes (free 2000/mo) | Codespace hours (free 60h/mo) |

## Timeouts

| Command | Timeout |
|---------|---------|
| Simple chat (Worker) | 30s (OpenAI API) |
| Codespace start | 120s (poll interval 5s) |
| /app | 15min |
| /issue, /research, chat (Codex) | 5min |
| /implement, /review | 30min |

## Future Extensions (Not in Scope)

- `/draw` — AI image generation
- `/translate` — translation
- `/download` — video download
- Dashboard (would need separate public repo or auth)
- Multiple Codespace pool for concurrent tasks
- Gemini as secondary fast-track provider
