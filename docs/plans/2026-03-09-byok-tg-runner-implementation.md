# byok-tg-runner Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a long-running Telegram bot on GitHub Actions with Copilot SDK BYOK connecting to Azure AI Foundry, adapted from aw-runner architecture.

**Architecture:** Public repo (byok-tg-runner) hosts infrastructure — CF Worker, GitHub Actions workflows, FastAPI server with Copilot SDK. Private repo (byok-tg-main) holds skills, prompts, tools, and conversation logs. Dual runner HA with mutual monitoring.

**Tech Stack:** Python 3.11+, FastAPI, github-copilot-sdk, Cloudflare Workers (TypeScript), cloudflared tunnel, GitHub Actions

---

### Task 1: Project scaffolding and .gitignore

**Files:**
- Create: `byok-tg-runner/.gitignore`
- Create: `byok-tg-runner/.env.example`

**Step 1: Create .gitignore**

```gitignore
# Python
__pycache__/
*.pyc
.venv/

# Node
node_modules/
worker/dist/

# Secrets
.env
.dev.vars
```

**Step 2: Create .env.example**

```
TELEGRAM_BOT_TOKEN=your-telegram-bot-token
FOUNDRY_API_KEY=your-azure-ai-foundry-api-key
RUNNER_API_KEY=your-runner-api-key
```

**Step 3: Commit**

```bash
cd /home/ct/copilot/byok-tg-runner
git add .gitignore .env.example
git commit -m "chore: project scaffolding"
```

---

### Task 2: FastAPI server with Copilot SDK BYOK

**Files:**
- Create: `server/main.py`
- Create: `server/tools.py`
- Create: `server/requirements.txt`
- Create: `prompt.md`

**Step 1: Create server/requirements.txt**

```
fastapi==0.115.0
uvicorn==0.30.6
httpx==0.27.2
github-copilot-sdk>=0.1.32
python-dotenv>=1.0.0
```

**Step 2: Create prompt.md (default system prompt, overwritten by private repo)**

```markdown
You are a helpful AI assistant.
Reply in the same language the user used.
Be concise and helpful.
```

**Step 3: Create server/tools.py**

```python
"""Custom tools for Copilot SDK sessions.

This file is the public repo default. The private repo (byok-tg-main)
can override it by placing its own tools/ directory.
"""

from copilot import define_tool
from pydantic import BaseModel, Field


class GetWeatherParams(BaseModel):
    city: str = Field(description="City name")


@define_tool(description="Get weather for a city (mock data)")
async def get_weather(params: GetWeatherParams) -> str:
    weather_data = {
        "台北": "晴天，28°C",
        "東京": "多雲，22°C",
        "紐約": "雨天，15°C",
    }
    result = weather_data.get(params.city, f"{params.city}: no data")
    return f"{params.city}: {result}"


# All tools to register with Copilot SDK sessions
ALL_TOOLS = [get_weather]
```

**Step 4: Create server/main.py**

```python
import asyncio
import logging
import os
import subprocess
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from copilot import CopilotClient, PermissionHandler

# Suppress conversation content from stdout (public repo Actions logs)
logging.basicConfig(level=logging.WARNING)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
RUNNER_API_KEY = os.environ["RUNNER_API_KEY"]
FOUNDRY_API_KEY = os.environ["FOUNDRY_API_KEY"]
FOUNDRY_BASE_URL = os.environ.get(
    "FOUNDRY_BASE_URL",
    "https://duotify-ai-foundry.cognitiveservices.azure.com/openai/v1",
)
MODEL = os.environ.get("MODEL", "gpt-5.2")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
LOG_REPO = os.environ.get("LOG_REPO", "")  # e.g. "yazelin/byok-tg-main"

START_TIME = time.time()
client: CopilotClient = None


def load_tools():
    """Load tools from server/tools.py."""
    try:
        from tools import ALL_TOOLS
        return ALL_TOOLS
    except ImportError:
        return []


def load_prompt():
    """Load system prompt from prompt.md."""
    try:
        with open("prompt.md") as f:
            return f.read()
    except FileNotFoundError:
        return "You are a helpful AI assistant."


@asynccontextmanager
async def lifespan(app: FastAPI):
    global client
    client = CopilotClient()
    await client.start()
    yield
    await client.stop()


app = FastAPI(lifespan=lifespan)


# --- Models ---

class TaskRequest(BaseModel):
    text: str
    chat_id: str


class TriggerRequest(BaseModel):
    prompt: str
    callback_repo: str = ""
    callback_workflow: str = ""
    context: str = ""


# --- Helpers ---

async def send_telegram(chat_id: str, text: str) -> None:
    """Send a message via Telegram Bot API."""
    async with httpx.AsyncClient() as http:
        for i in range(0, len(text), 4096):
            await http.post(
                f"{TELEGRAM_API}/sendMessage",
                json={"chat_id": chat_id, "text": text[i:i + 4096]},
                timeout=10,
            )


async def run_copilot_sdk(prompt_text: str) -> str:
    """Run a single Copilot SDK session and return the reply."""
    system_prompt = load_prompt()
    tools = load_tools()

    async with await client.create_session({
        "model": MODEL,
        "provider": {
            "type": "openai",
            "base_url": FOUNDRY_BASE_URL,
            "api_key": FOUNDRY_API_KEY,
            "wire_api": "responses",
        },
        "tools": tools,
        "on_permission_request": PermissionHandler.approve_all,
    }) as session:
        done = asyncio.Event()
        reply_parts = []

        def on_event(event):
            t = event.type.value
            if t == "assistant.message":
                reply_parts.append(event.data.content or "")
            elif t == "session.idle":
                done.set()

        session.on(on_event)
        full_prompt = f"{system_prompt}\n\n{prompt_text}"
        await session.send({"prompt": full_prompt})
        await done.wait()

    return "\n".join(reply_parts) or "(no response)"


async def log_to_private_repo(title: str, body: str) -> None:
    """Create an issue in the private log repo (fire-and-forget)."""
    if not LOG_REPO:
        return
    try:
        proc = await asyncio.create_subprocess_exec(
            "gh", "issue", "create",
            "--repo", LOG_REPO,
            "--title", title,
            "--body", body,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
    except Exception:
        pass  # Don't fail the main flow


async def callback_workflow(repo: str, workflow: str, result: str, context: str) -> None:
    """Trigger a workflow_dispatch on another repo."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "gh", "workflow", "run", workflow,
            "--repo", repo,
            "-f", f"result={result}",
            "-f", f"context={context}",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
    except Exception:
        pass


# --- Endpoints ---

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/status")
async def status():
    elapsed = int(time.time() - START_TIME)
    hours, rem = divmod(elapsed, 3600)
    minutes, seconds = divmod(rem, 60)
    return {
        "status": "ok",
        "uptime_seconds": elapsed,
        "uptime": f"{hours}h {minutes}m {seconds}s",
    }


@app.post("/task")
async def task(req: TaskRequest, x_api_key: str = Header(...)):
    if x_api_key != RUNNER_API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
    asyncio.create_task(_process_task(req))
    print(f"[task] accepted chat_id={req.chat_id}")  # Safe: no content logged
    return {"status": "accepted"}


@app.post("/trigger")
async def trigger(req: TriggerRequest, x_api_key: str = Header(...)):
    if x_api_key != RUNNER_API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
    task_id = f"trg-{int(time.time())}"
    asyncio.create_task(_process_trigger(req, task_id))
    print(f"[trigger] accepted task_id={task_id}")
    return {"status": "accepted", "task_id": task_id}


# --- Background processors ---

async def _process_task(req: TaskRequest) -> None:
    """Process a Telegram message."""
    try:
        reply = await run_copilot_sdk(req.text)
        await send_telegram(req.chat_id, reply)
        print(f"[task] completed chat_id={req.chat_id}")
        # Log to private repo
        await log_to_private_repo(
            f"[tg] chat={req.chat_id}",
            f"**User:** {req.text}\n\n**Assistant:** {reply}",
        )
    except Exception as e:
        print(f"[task] error chat_id={req.chat_id} err={type(e).__name__}")
        await send_telegram(req.chat_id, f"Error: {e}")


async def _process_trigger(req: TriggerRequest, task_id: str) -> None:
    """Process an external trigger."""
    try:
        reply = await run_copilot_sdk(req.prompt)
        print(f"[trigger] completed task_id={task_id}")
        # Callback if configured
        if req.callback_repo and req.callback_workflow:
            await callback_workflow(
                req.callback_repo, req.callback_workflow, reply, req.context,
            )
        # Log to private repo
        await log_to_private_repo(
            f"[trigger] {task_id} ctx={req.context}",
            f"**Prompt:** {req.prompt}\n\n**Result:** {reply}",
        )
    except Exception as e:
        print(f"[trigger] error task_id={task_id} err={type(e).__name__}")
        if req.callback_repo and req.callback_workflow:
            await callback_workflow(
                req.callback_repo, req.callback_workflow, f"Error: {e}", req.context,
            )
```

**Step 5: Commit**

```bash
git add server/ prompt.md
git commit -m "feat: FastAPI server with Copilot SDK BYOK and /trigger endpoint"
```

---

### Task 3: Cloudflare Worker

**Files:**
- Create: `worker/package.json`
- Create: `worker/wrangler.toml`
- Create: `worker/src/index.ts`

**Step 1: Create worker/package.json**

```json
{
  "name": "byok-tg-runner-worker",
  "version": "1.0.0",
  "private": true,
  "scripts": {
    "dev": "wrangler dev",
    "deploy": "wrangler deploy"
  },
  "devDependencies": {
    "@cloudflare/workers-types": "^4.20240821.1",
    "typescript": "^5.5.4",
    "wrangler": "^3.70.0"
  }
}
```

**Step 2: Create worker/wrangler.toml**

```toml
name = "byok-tg-runner-worker"
main = "src/index.ts"
compatibility_date = "2024-09-02"

[[kv_namespaces]]
binding = "RUNNER_KV"
id = "PLACEHOLDER_REPLACE_DURING_SETUP"

# Secrets (set via wrangler secret put):
# RUNNER_API_KEY       - shared key with FastAPI
# TELEGRAM_BOT_TOKEN   - for optional direct reply on error
# ALLOWED_CHAT_ID      - Telegram chat whitelist
```

**Step 3: Create worker/src/index.ts**

Adapted from aw-runner — adds `/trigger` route forwarding.

```typescript
export interface Env {
  RUNNER_KV: KVNamespace;
  RUNNER_API_KEY: string;
  TELEGRAM_BOT_TOKEN: string;
  ALLOWED_CHAT_ID: string;
}

interface TelegramUpdate {
  message?: {
    chat: { id: number };
    text?: string;
  };
}

/** Try runner URLs in order, return first reachable one */
async function getActiveRunner(env: Env): Promise<string | null> {
  const keys = ["runner_a_url", "runner_b_url"];
  const urls: string[] = [];

  for (const key of keys) {
    const url = await env.RUNNER_KV.get(key);
    if (url) urls.push(url);
  }

  if (urls.length === 0) return null;

  const checks = urls.map(async (url) => {
    try {
      const res = await fetch(`${url}/health`, { signal: AbortSignal.timeout(4000) });
      if (res.ok) return url;
    } catch {}
    return null;
  });

  const results = await Promise.all(checks);
  for (const r of results) {
    if (r) return r;
  }
  return urls[0];
}

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);

    const corsHeaders = {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    };

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders });
    }

    // GET /status — proxy to runners
    if (request.method === "GET" && url.pathname === "/status") {
      const runnerAUrl = await env.RUNNER_KV.get("runner_a_url");
      const runnerBUrl = await env.RUNNER_KV.get("runner_b_url");

      const tryStatus = async (runnerUrl: string | null, slot: string) => {
        if (!runnerUrl) return { slot, status: "no_url" as const, url: null, data: null };
        try {
          const res = await fetch(`${runnerUrl}/status`, { signal: AbortSignal.timeout(4000) });
          const data = await res.json();
          return { slot, status: "ok" as const, url: runnerUrl, data };
        } catch {
          return { slot, status: "unreachable" as const, url: runnerUrl, data: null };
        }
      };

      const [statusA, statusB] = await Promise.all([
        tryStatus(runnerAUrl, "a"),
        tryStatus(runnerBUrl, "b"),
      ]);

      const active = statusA.status === "ok" ? statusA : statusB.status === "ok" ? statusB : null;

      return Response.json({
        status: active ? "ok" : "offline",
        active_slot: active?.slot ?? null,
        runner_a: { status: statusA.status, url: statusA.url },
        runner_b: { status: statusB.status, url: statusB.url },
        ...(active?.data as object ?? {}),
      }, {
        status: active ? 200 : 503,
        headers: corsHeaders,
      });
    }

    // POST /trigger — external API trigger (authenticated)
    if (request.method === "POST" && url.pathname === "/trigger") {
      const apiKey = request.headers.get("x-api-key");
      if (apiKey !== env.RUNNER_API_KEY) {
        return new Response("Unauthorized", { status: 401 });
      }

      const runnerUrl = await getActiveRunner(env);
      if (!runnerUrl) {
        return Response.json({ status: "error", message: "No runner available" }, { status: 503 });
      }

      const body = await request.text();

      ctx.waitUntil(
        fetch(`${runnerUrl}/trigger`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "x-api-key": env.RUNNER_API_KEY,
          },
          body,
        })
      );

      return Response.json({ status: "accepted" });
    }

    if (request.method !== "POST") {
      return new Response("Method Not Allowed", { status: 405 });
    }

    // POST (Telegram webhook)
    let update: TelegramUpdate;
    try {
      update = await request.json();
    } catch {
      return new Response("Bad Request", { status: 400 });
    }

    const message = update.message;
    if (!message?.text) {
      return new Response("OK", { status: 200 });
    }

    const chat_id = String(message.chat.id);
    if (chat_id !== env.ALLOWED_CHAT_ID) {
      return new Response("OK", { status: 200 });
    }

    const runnerUrl = await getActiveRunner(env);
    if (!runnerUrl) {
      return new Response("Runner not available", { status: 503 });
    }

    const text = message.text;

    ctx.waitUntil(
      fetch(`${runnerUrl}/task`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "x-api-key": env.RUNNER_API_KEY,
        },
        body: JSON.stringify({ text, chat_id }),
      })
    );

    return new Response("OK", { status: 200 });
  },
} satisfies ExportedHandler<Env>;
```

**Step 4: Commit**

```bash
git add worker/
git commit -m "feat: Cloudflare Worker with /trigger route and dual runner health check"
```

---

### Task 4: GitHub Actions workflows (dual runner)

**Files:**
- Create: `.github/workflows/runner-a.yml`
- Create: `.github/workflows/runner-b.yml`

**Step 1: Create .github/workflows/runner-a.yml**

```yaml
name: runner-a

on:
  workflow_dispatch:

jobs:
  serve:
    runs-on: ubuntu-latest
    timeout-minutes: 350

    steps:
      - uses: actions/checkout@v4

      - name: Install cloudflared
        run: |
          curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
            | sudo gpg --dearmor -o /usr/share/keyrings/cloudflare-main.gpg
          echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared focal main' \
            | sudo tee /etc/apt/sources.list.d/cloudflared.list
          sudo apt-get update -q && sudo apt-get install -y cloudflared

      - name: Install Copilot CLI
        run: npm install -g @github/copilot
        env:
          COPILOT_GITHUB_TOKEN: ${{ secrets.GH_PAT }}

      - name: Pull private configs
        run: |
          git clone https://x-access-token:${{ secrets.GH_PAT }}@github.com/yazelin/byok-tg-main.git /tmp/private
          # Override prompt
          [ -f /tmp/private/prompts/system.md ] && cp /tmp/private/prompts/system.md prompt.md
          # Override tools
          [ -d /tmp/private/tools ] && cp -r /tmp/private/tools/* server/ 2>/dev/null || true
          # Copy skills
          [ -d /tmp/private/skills ] && cp -r /tmp/private/skills server/ 2>/dev/null || true
          # Copy MCP config
          [ -f /tmp/private/mcp-config.json ] && mkdir -p ~/.copilot && cp /tmp/private/mcp-config.json ~/.copilot/mcp-config.json
          rm -rf /tmp/private
        env:
          GH_TOKEN: ${{ secrets.GH_PAT }}

      - name: Install Python deps
        run: pip install -r server/requirements.txt

      - name: Start FastAPI server
        run: uvicorn server.main:app --host 0.0.0.0 --port 8000 > /dev/null 2>&1 &
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          RUNNER_API_KEY: ${{ secrets.RUNNER_API_KEY }}
          FOUNDRY_API_KEY: ${{ secrets.FOUNDRY_API_KEY }}
          LOG_REPO: yazelin/byok-tg-main
          GH_TOKEN: ${{ secrets.GH_PAT }}

      - name: Wait for server to start
        run: sleep 5

      - name: Verify server health
        run: curl -sf http://localhost:8000/health

      - name: Start cloudflared quick tunnel and register URL
        run: |
          cloudflared tunnel --url http://localhost:8000 --logfile /tmp/cf.log &
          sleep 10
          TUNNEL_URL=$(grep -oP 'https://\S+\.trycloudflare\.com' /tmp/cf.log | head -1)
          echo "Tunnel URL: $TUNNEL_URL"
          curl -sf -X PUT \
            "https://api.cloudflare.com/client/v4/accounts/${{ secrets.CF_ACCOUNT_ID }}/storage/kv/namespaces/${{ secrets.KV_NAMESPACE_ID }}/values/runner_a_url" \
            -H "Authorization: Bearer ${{ secrets.CF_API_TOKEN }}" \
            -d "$TUNNEL_URL"
        env:
          NO_AUTOUPDATE: "true"

      - name: Notify runner-a online
        run: |
          TUNNEL_URL=$(grep -oP 'https://\S+\.trycloudflare\.com' /tmp/cf.log | head -1)
          RUN_URL="https://github.com/${{ github.repository }}/actions/runs/${{ github.run_id }}"
          curl -sf -X POST "https://api.telegram.org/bot${{ secrets.TELEGRAM_BOT_TOKEN }}/sendMessage" \
            -H "Content-Type: application/json" \
            -d "{\"chat_id\":\"${{ secrets.TELEGRAM_CHAT_ID }}\",\"text\":\"🟢 byok runner-a online\nTunnel: $TUNNEL_URL\nRun: $RUN_URL\"}"

      - name: Monitor loop
        run: |
          SLOT="a"
          PARTNER_WORKFLOW="runner-b.yml"
          INTERVAL=300
          MAX_LOOPS=66
          TRIGGER_AFTER=30

          for i in $(seq 1 $MAX_LOOPS); do
            echo "=== [$SLOT] Check #$i/$MAX_LOOPS at $(date -u) ==="

            PARTNER_COUNT=$(gh run list --workflow=$PARTNER_WORKFLOW \
              --repo ${{ github.repository }} \
              --status in_progress --json databaseId -q 'length')
            PARTNER_QUEUED=$(gh run list --workflow=$PARTNER_WORKFLOW \
              --repo ${{ github.repository }} \
              --status queued --json databaseId -q 'length')
            PARTNER_TOTAL=$((PARTNER_COUNT + PARTNER_QUEUED))
            echo "Partner: in_progress=$PARTNER_COUNT queued=$PARTNER_QUEUED"

            if [ "$PARTNER_TOTAL" -eq 0 ] && [ "$i" -ge "$TRIGGER_AFTER" ]; then
              echo "Triggering partner..."
              gh workflow run $PARTNER_WORKFLOW --repo ${{ github.repository }}
            elif [ "$PARTNER_TOTAL" -eq 0 ]; then
              echo "Partner not running, too early to trigger (loop #$i < #$TRIGGER_AFTER)"
            else
              echo "Partner is alive"
            fi

            sleep $INTERVAL
          done

          echo "Runner-$SLOT loop finished, exiting gracefully"
        env:
          GH_TOKEN: ${{ secrets.GH_PAT }}
```

**Step 2: Create .github/workflows/runner-b.yml**

Same as runner-a.yml with these changes:
- `name: runner-b`
- `SLOT="b"`
- `PARTNER_WORKFLOW="runner-a.yml"`
- KV key: `runner_b_url` instead of `runner_a_url`
- Notify message: `byok runner-b online`

**Step 3: Commit**

```bash
git add .github/
git commit -m "feat: dual runner GitHub Actions workflows with private repo pull"
```

---

### Task 5: Setup script

**Files:**
- Create: `scripts/setup.sh`

**Step 1: Create scripts/setup.sh**

Adapted from aw-runner setup.sh with these changes:
- Remove `COPILOT_GITHUB_TOKEN` step, replace with `FOUNDRY_API_KEY`
- Add `ALLOWED_CHAT_ID` as Wrangler secret
- Worker name: `byok-tg-runner-worker`
- Repo name default: `byok-tg-runner`
- Setup file checks: `runner-a.yml` instead of `runner.yml`
- Trigger `runner-a.yml` at final step

**Step 2: Commit**

```bash
git add scripts/
git commit -m "feat: interactive setup script"
```

---

### Task 6: Private repo (byok-tg-main) scaffolding

**Files:**
- Create new repo: `byok-tg-main` (private)
- Create: `prompts/system.md`
- Create: `tools/` (placeholder)
- Create: `skills/` (placeholder)
- Create: `README.md`

**Step 1: Create directory structure**

```bash
mkdir -p /home/ct/copilot/byok-tg-main/{prompts,tools,skills}
```

**Step 2: Create prompts/system.md**

```markdown
You are a helpful AI assistant running as a Telegram bot.
Reply in the same language the user used.
Be concise and helpful.
```

**Step 3: Create README.md**

```markdown
# byok-tg-main

Private configuration repo for byok-tg-runner.

## Structure

- `prompts/system.md` — System prompt for the AI
- `tools/` — Custom tool definitions (Python, loaded by server)
- `skills/` — Skill definitions
- `mcp-config.json` — MCP server configuration (optional)

## Logs

Conversation logs are stored as GitHub Issues in this repo.
```

**Step 4: Init, commit, and push as private repo**

```bash
cd /home/ct/copilot/byok-tg-main
git init
git add -A
git commit -m "Initial commit: private config repo for byok-tg-runner"
gh repo create byok-tg-main --private --source=. --push
```

---

### Task 7: Push public repo to GitHub

**Step 1: Push byok-tg-runner**

```bash
cd /home/ct/copilot/byok-tg-runner
gh repo create byok-tg-runner --public --source=. --push
```

---

### Task 8: Run setup script

**Step 1: Install worker dependencies**

```bash
cd /home/ct/copilot/byok-tg-runner/worker
npm install
```

**Step 2: Run setup**

```bash
cd /home/ct/copilot/byok-tg-runner
bash scripts/setup.sh
```

This will interactively:
1. Check prerequisites
2. Configure Cloudflare KV
3. Collect secrets (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, CF_API_TOKEN, FOUNDRY_API_KEY, GH_PAT)
4. Auto-generate RUNNER_API_KEY
5. Set GitHub Secrets
6. Deploy CF Worker + set Wrangler secrets
7. Set Telegram webhook
8. Trigger first runner
