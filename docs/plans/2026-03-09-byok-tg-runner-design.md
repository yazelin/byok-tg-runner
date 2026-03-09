# byok-tg-runner Design Document

## Overview

A long-running Telegram bot powered by GitHub Copilot SDK with BYOK (Bring Your Own Key) mode, connecting to Azure AI Foundry. Runs on GitHub Actions with dual-runner HA architecture, adapted from [aw-runner](https://github.com/yazelin/aw-runner).

## Architecture

```
                    Trigger Sources
                    ┌──────────┬──────────────────────────┐
                    │ Telegram │   Private repo Actions    │
                    │ webhook  │   (curl /trigger)         │
                    └────┬─────┴──────────┬───────────────┘
                         │                │
                         ▼                ▼
                    CF Worker
                    - Telegram: chat_id whitelist
                    - /trigger: x-api-key auth
                    - Health check dual runners, pick active
                         │
                    ┌────┴────┐
                    ▼         ▼
                Runner A   Runner B     (GitHub Actions, 5.5h loop)
                    └────┬────┘          Mutual monitoring
                         ▼
                    FastAPI Server
                    POST /task    → Telegram message handling
                    POST /trigger → External trigger handling
                    GET  /health  → Liveness probe
                    GET  /status  → Uptime info
                         │
                         ▼
                    Copilot SDK (in-process)
                    - BYOK → Azure AI Foundry gpt-5.2
                    - @define_tool custom tools
                    - PermissionHandler.approve_all
                         │
                    ┌────┴────┐
                    ▼         ▼
              Telegram    gh workflow run
              reply       (callback to private repo)
```

## Key Differences from aw-runner

| Component | aw-runner | byok-tg-runner |
|-----------|-----------|----------------|
| AI invocation | `subprocess: copilot CLI` | `Copilot SDK Python (in-process)` |
| AI auth | `COPILOT_GITHUB_TOKEN` | `FOUNDRY_API_KEY` (BYOK) |
| Provider | GitHub Copilot API | Azure AI Foundry |
| Model | Per Copilot subscription | `gpt-5.2` (user's deployment) |
| Reply method | Copilot calls `send_telegram_message.py` | FastAPI sends via httpx |
| Tools | Copilot built-in (shell, file) | `@define_tool` custom tools |
| Log handling | Visible in public Actions log | Silent mode + logs forwarded to private repo |

**Unchanged (copied from aw-runner):**
- CF Worker (webhook + chat_id filter + dual runner health check)
- CF KV (store runner URLs)
- GitHub Actions dual workflow mutual monitoring (5.5h loop)
- cloudflared tunnel

## Public/Private Repo Split

```
Public repo (byok-tg-runner)         Private repo (byok-tg-main)
├── .github/workflows/               ├── skills/          ← Skill definitions
├── server/main.py                   ├── mcp-config.json  ← MCP server config
├── worker/                          ├── prompts/         ← System prompts
└── Infrastructure only,             ├── tools/           ← Custom tool code
    no sensitive content              └── (issues)         ← AI conversation logs
```

Workflow pulls private configs at startup:
```yaml
- name: Pull private configs
  run: |
    git clone https://x-access-token:${{ secrets.GH_PAT }}@github.com/yazelin/byok-tg-main.git /tmp/private
    cp -r /tmp/private/skills ./server/
    cp -r /tmp/private/tools ./server/
    cp /tmp/private/mcp-config.json ./server/
    cp /tmp/private/prompts/system.md ./prompt.md
    rm -rf /tmp/private
```

## Project Structure

```
byok-tg-runner/
├── .github/
│   └── workflows/
│       ├── runner-a.yml
│       └── runner-b.yml
├── server/
│   ├── main.py                   # FastAPI + Copilot SDK BYOK
│   ├── tools.py                  # Example custom tools
│   └── requirements.txt
├── worker/
│   ├── src/index.ts              # CF Worker
│   ├── wrangler.toml
│   └── package.json
├── scripts/
│   └── setup.sh
├── docs/
│   └── index.html                # Status dashboard
├── prompt.md                     # System prompt (overwritten from private repo)
├── .env.example
├── .gitignore
└── README.md
```

## Endpoints

| Endpoint | Method | Source | Auth | Response |
|----------|--------|--------|------|----------|
| `/task` | POST | CF Worker (Telegram) | `x-api-key` | Telegram sendMessage |
| `/trigger` | POST | CF Worker / direct curl | `x-api-key` | `gh workflow run` callback |
| `/health` | GET | CF Worker health check | None | `{"status":"ok"}` |
| `/status` | GET | Dashboard | None | Uptime info |

## /trigger Request & Callback Flow

**Request:**
```json
{
  "prompt": "Analyze this issue...",
  "callback_repo": "yazelin/my-private-repo",
  "callback_workflow": "on-ai-result.yml",
  "context": "issue-123"
}
```

**Callback (after processing):**
```bash
gh workflow run on-ai-result.yml \
  --repo yazelin/my-private-repo \
  -f result="<AI reply>" \
  -f context="issue-123"
```

## Private Repo Lightweight Forwarding

Private repos can trigger AI processing with a simple curl (< 5 seconds):

```yaml
# Private repo: .github/workflows/forward-to-ai.yml
on:
  issues:
    types: [opened]
jobs:
  forward:
    runs-on: ubuntu-latest
    steps:
      - run: |
          curl -s -X POST "${{ secrets.BYOK_RUNNER_URL }}/trigger" \
            -H "Content-Type: application/json" \
            -H "x-api-key: ${{ secrets.RUNNER_API_KEY }}" \
            -d '{
              "prompt": "Analyze: ${{ github.event.issue.title }}",
              "callback_repo": "${{ github.repository }}",
              "callback_workflow": "on-ai-result.yml",
              "context": "issue-${{ github.event.issue.number }}"
            }'
```

## Secrets (8 total)

| Secret | Purpose | Set In |
|--------|---------|--------|
| `TELEGRAM_BOT_TOKEN` | Send Telegram replies | GitHub Secrets + CF Worker |
| `TELEGRAM_CHAT_ID` | CF Worker whitelist | GitHub Secrets + CF Worker |
| `FOUNDRY_API_KEY` | Azure AI Foundry BYOK | GitHub Secrets |
| `RUNNER_API_KEY` | Worker/external → FastAPI auth | GitHub Secrets + CF Worker |
| `CF_ACCOUNT_ID` | KV write | GitHub Secrets |
| `CF_API_TOKEN` | KV write | GitHub Secrets |
| `KV_NAMESPACE_ID` | KV namespace | GitHub Secrets |
| `GH_PAT` | Dual runner trigger + callback to private repos | GitHub Secrets |

## Log Handling (Silent Mode)

- FastAPI server outputs **no conversation content** to stdout
- Actions log only shows: task received, task completed, duration
- Full conversation logs forwarded to `byok-tg-main` private repo as issues
- Error details also go to private repo

## Server Core Logic

```python
# CopilotClient lifecycle: start with FastAPI, stop on shutdown
@asynccontextmanager
async def lifespan(app):
    global client
    client = CopilotClient()
    await client.start()
    yield
    await client.stop()

# /task handler (Telegram)
async def _process_task(req):
    async with await client.create_session({
        "model": "gpt-5.2",
        "provider": {"type": "openai", "base_url": FOUNDRY_BASE_URL, ...},
        "tools": tools_list,
        "on_permission_request": PermissionHandler.approve_all,
    }) as session:
        # ... event handling, collect reply
    await send_telegram(req.chat_id, reply)

# /trigger handler (external)
async def _process_trigger(req):
    # ... same Copilot SDK processing
    # callback via gh workflow run
    subprocess.run(["gh", "workflow", "run", req.callback_workflow,
                    "--repo", req.callback_repo,
                    "-f", f"result={reply}", "-f", f"context={req.context}"])
```

## Provider Config

```python
{
    "type": "openai",
    "base_url": "https://duotify-ai-foundry.cognitiveservices.azure.com/openai/v1",
    "api_key": os.environ["FOUNDRY_API_KEY"],
    "wire_api": "responses",
}
```

## HA Strategy

- Runner A and Runner B: 5.5h loop each, mutual monitoring via `gh run list`
- After 2.5h, if partner is dead, trigger it via `gh workflow run`
- Staggered ~3h offset ensures continuous coverage
- CF Worker races health checks on both runners, picks first healthy one
