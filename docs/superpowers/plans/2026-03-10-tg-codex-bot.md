# tg-codex-bot Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Telegram bot backed by Cloudflare Worker (simple chat via OpenAI API) and GitHub Codespace (complex tasks via Codex CLI), all in a single private repo with one-click setup.

**Architecture:** CF Worker handles webhook, routing, KV memory, OpenAI chat, and Codespace lifecycle. Task server inside Codespace receives work, runs `codex -q -a full-auto`, returns results. Worker stops Codespace immediately after task completion.

**Tech Stack:** TypeScript (CF Worker), Python (task server), OpenAI Codex CLI, GitHub Codespace API, Cloudflare KV

**Spec:** `docs/superpowers/specs/2026-03-10-tg-codex-bot-design.md`

---

## Chunk 1: New Repo Scaffold + Devcontainer + Task Server

### Task 1: Create repo and scaffold directory structure

**Files:**
- Create: `tg-codex-bot/` (new private repo root — work in `/home/ct/copilot/tg-codex-bot/`)

- [ ] **Step 1: Create private GitHub repo and clone**

```bash
gh repo create yazelin/tg-codex-bot --private --clone
cd /home/ct/copilot/tg-codex-bot
```

- [ ] **Step 2: Create directory structure**

```bash
mkdir -p .devcontainer worker/src server prompts templates/workflows config scripts
```

- [ ] **Step 3: Create .gitignore**

Create `.gitignore`:
```
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

# Codex
.codex/
```

- [ ] **Step 4: Create AGENTS.md**

Create `AGENTS.md` — Codex CLI reads this automatically for project conventions:
```markdown
# tg-codex-bot

Telegram bot with CF Worker gateway and Codespace-based Codex CLI agent.

## Structure
- `worker/` — Cloudflare Worker (TypeScript). Handles webhook, routing, OpenAI simple chat, Codespace lifecycle.
- `server/` — Task server (Python/FastAPI). Runs inside Codespace. Receives tasks, invokes Codex CLI.
- `prompts/` — System prompts.
- `templates/workflows/` — GitHub Actions templates injected into child repos.
- `config/` — Codex CLI config.
- `scripts/` — Setup and maintenance scripts.

## Conventions
- Traditional Chinese (繁體中文) for user-facing text
- All Telegram messages ≤ 4096 chars
- App repos created under configured APPS_ORG
- Issues use copilot-task label
```

- [ ] **Step 5: Commit scaffold**

```bash
git add -A
git commit -m "chore: initial repo scaffold"
git push
```

---

### Task 2: Devcontainer configuration

**Files:**
- Create: `.devcontainer/devcontainer.json`
- Create: `.devcontainer/post-start.sh`

- [ ] **Step 1: Create devcontainer.json**

Create `.devcontainer/devcontainer.json`:
```json
{
  "name": "tg-codex-bot",
  "image": "mcr.microsoft.com/devcontainers/universal:2",
  "features": {
    "ghcr.io/devcontainers/features/github-cli:1": {},
    "ghcr.io/devcontainers/features/node:1": {}
  },
  "postCreateCommand": "bash -c 'npm install -g @openai/codex && pip install fastapi uvicorn httpx && mkdir -p ~/.codex && cp config/codex-config.yaml ~/.codex/config.yaml'",
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

- [ ] **Step 2: Create post-start.sh**

Create `.devcontainer/post-start.sh`:
```bash
#!/bin/bash
set -e
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

# Start task server in background
echo "[post-start] Starting task server on :8080..."
nohup uvicorn server.main:app --host 0.0.0.0 --port 8080 > /tmp/task-server.log 2>&1 &

# Wait for server to be ready
for i in $(seq 1 10); do
  if curl -sf http://localhost:8080/health > /dev/null 2>&1; then
    echo "[post-start] Task server ready"
    exit 0
  fi
  sleep 1
done
echo "[post-start] WARNING: Task server not ready after 10s"
cat /tmp/task-server.log
```

```bash
chmod +x .devcontainer/post-start.sh
```

- [ ] **Step 3: Commit**

```bash
git add .devcontainer/
git commit -m "feat: add devcontainer config with task server auto-start"
```

---

### Task 3: Codex CLI configuration

**Files:**
- Create: `config/codex-config.yaml`

- [ ] **Step 1: Create codex-config.yaml**

Create `config/codex-config.yaml`:
```yaml
model: gpt-5.3-codex
provider: openai
approvalMode: full-auto
fullAutoErrorMode: ignore-and-continue
notify: false
```

- [ ] **Step 2: Commit**

```bash
git add config/
git commit -m "feat: add Codex CLI config (full-auto, gpt-5.3-codex)"
```

---

### Task 4: System prompt

**Files:**
- Create: `prompts/system.md`

- [ ] **Step 1: Create system prompt**

Create `prompts/system.md`. This is used in two contexts:
1. Worker sends a trimmed version to OpenAI API for simple chat
2. Codex CLI reads the full version for complex tasks

```markdown
# tg-codex-bot System Prompt

You are a helpful, friendly AI assistant responding to Telegram messages.

## Language & Format
- Respond in Traditional Chinese (繁體中文) unless the user writes in another language
- Keep responses under 4096 characters (Telegram limit)
- Be concise and practical

## Routing (for simple chat mode only)
If the user's request requires ANY of the following, respond with ONLY the text `<<<ROUTE_TO_CODEX>>>` and nothing else:
- Creating or modifying GitHub repositories
- Writing, editing, or reviewing code
- Running shell commands or build tools
- Multi-step implementation tasks
- Creating GitHub issues or PRs
- Web research requiring multiple sources
- Any task that needs file system access

## Commands (handled by Codex when routed)
- `/app <description>` — Create a new project (App Factory)
- `/app fork:<owner/repo> <description>` — Fork and customize
- `/issue <owner/repo> <description>` — Create structured issue
- `/research <topic>` — Research and synthesize information

## App Factory Rules (for /app)
1. Evaluate feasibility as MVP (or full product if user pref says so)
2. Tech simplicity: static > backend, native > framework, localStorage > database, zero deps preferred
3. Plan: README.md, AGENTS.md, 2-5 issues (foundation → implementation → polish)
4. Execute: create repo, push scaffold, create issues, set secrets
5. Repos under configured APPS_ORG

## Issue Format (for /issue)
Structure: Objective / Context / Approach / Files / Acceptance Criteria / Validation

## General Chat
- Answer questions directly
- If unsure whether task needs Codex, answer it yourself first
- Only route to Codex when you genuinely cannot handle it without tools
```

- [ ] **Step 2: Commit**

```bash
git add prompts/
git commit -m "feat: add system prompt with routing and app factory rules"
```

---

### Task 5: Task server — health endpoint and main structure

**Files:**
- Create: `server/main.py`
- Create: `server/requirements.txt`

- [ ] **Step 1: Create requirements.txt**

Create `server/requirements.txt`:
```
fastapi==0.115.0
uvicorn==0.30.6
httpx==0.27.2
```

- [ ] **Step 2: Create server/main.py with health endpoint**

Create `server/main.py`:
```python
import asyncio
import json
import os
import time

import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

TASK_API_KEY = os.environ.get("TASK_API_KEY", "")
GH_PAT = os.environ.get("GH_PAT", "")
APPS_ORG = os.environ.get("APPS_ORG", "aw-apps")
START_TIME = time.time()

app = FastAPI()


# --- Models ---

class TaskRequest(BaseModel):
    prompt: str
    command: str = "chat"       # app|issue|research|implement|chat
    chat_id: str = ""
    repo: str = ""              # for implement
    action: str = ""            # implement|fix-pr|review
    issue_number: int = 0
    pr_number: int = 0
    notify_chat_id: str = ""


class TaskResponse(BaseModel):
    status: str
    reply: str = ""
    error: str = ""


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


@app.post("/task", response_model=TaskResponse)
async def task(req: TaskRequest, x_api_key: str = Header(...)):
    if TASK_API_KEY and x_api_key != TASK_API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        from server.codex_runner import run_codex_task
        reply = await run_codex_task(req)
        # Log conversation as GitHub Issue
        await _log_to_issue(req, reply)
        return TaskResponse(status="ok", reply=reply)
    except Exception as e:
        return TaskResponse(status="error", error=str(e))


async def _log_to_issue(req: TaskRequest, reply: str) -> None:
    """Log conversation as GitHub Issue in this repo."""
    if not GH_PAT:
        return
    try:
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).strftime("%H:%M")
        title = f"[tg] {ts} cmd={req.command}"
        body = f"**User:** {req.prompt[:500]}\n\n**Assistant:** {reply[:1500]}"
        proc = await asyncio.create_subprocess_exec(
            "gh", "issue", "create",
            "--title", title,
            "--body", body,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
    except Exception:
        pass
```

- [ ] **Step 3: Verify syntax**

```bash
cd /home/ct/copilot/tg-codex-bot
python3 -c "import ast; ast.parse(open('server/main.py').read()); print('OK')"
```

- [ ] **Step 4: Commit**

```bash
git add server/main.py server/requirements.txt
git commit -m "feat: task server with health, status, and /task endpoint"
```

---

### Task 6: Codex CLI runner

**Files:**
- Create: `server/codex_runner.py`

- [ ] **Step 1: Create codex_runner.py**

Create `server/codex_runner.py`:
```python
"""Wrapper to invoke Codex CLI and capture output."""

import asyncio
import json
import os
import shutil
import tempfile

CODEX_MODEL = os.environ.get("CODEX_MODEL", "gpt-5.3-codex")


def _load_system_prompt() -> str:
    """Load system prompt from prompts/system.md."""
    paths = [
        os.path.join(os.path.dirname(__file__), "..", "prompts", "system.md"),
        "prompts/system.md",
    ]
    for p in paths:
        try:
            with open(p) as f:
                return f.read()
        except FileNotFoundError:
            continue
    return "You are a helpful AI assistant."


async def run_codex(prompt: str, cwd: str | None = None,
                    timeout: int = 300) -> str:
    """Run codex CLI in quiet full-auto mode. Returns stdout text."""
    cmd = [
        "codex", "-q",
        "-a", "full-auto",
        "-m", CODEX_MODEL,
        prompt,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "CODEX_QUIET_MODE": "1"},
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise RuntimeError(f"Codex timed out after {timeout}s")

    output = stdout.decode(errors="replace").strip()
    if proc.returncode != 0 and not output:
        err = stderr.decode(errors="replace").strip()
        raise RuntimeError(f"Codex failed (rc={proc.returncode}): {err[:500]}")
    return output or "(no output)"


async def run_codex_task(req) -> str:
    """Route a TaskRequest to the appropriate codex invocation."""
    from server.main import TaskRequest
    assert isinstance(req, TaskRequest)

    system_prompt = _load_system_prompt()

    if req.command == "implement":
        return await _handle_implement(req, system_prompt)

    # For app, issue, research, chat: build prompt and run codex
    history_section = ""
    if req.prompt.startswith("--- Chat History ---"):
        # History already embedded in prompt
        history_section = ""

    timeout = 900 if req.command == "app" else 300
    full_prompt = f"{system_prompt}\n\n{req.prompt}"
    return await run_codex(full_prompt, timeout=timeout)


async def _handle_implement(req, system_prompt: str) -> str:
    """Clone repo, run codex with full access, handle implement/fix-pr/review."""
    if not req.repo:
        raise ValueError("repo is required for implement")

    gh_pat = os.environ.get("GH_PAT", "")
    tmpdir = tempfile.mkdtemp(prefix="impl-")

    try:
        # Clone repo
        clone_url = f"https://x-access-token:{gh_pat}@github.com/{req.repo}.git"
        proc = await asyncio.create_subprocess_exec(
            "git", "clone", clone_url, tmpdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"git clone failed: {stderr.decode()[:300]}")

        # Configure git
        for cmd in [
            ["git", "config", "user.name", "codex-bot"],
            ["git", "config", "user.email", "codex-bot@users.noreply.github.com"],
        ]:
            p = await asyncio.create_subprocess_exec(
                *cmd, cwd=tmpdir,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await p.communicate()

        # Build prompt based on action
        if req.action == "implement":
            prompt = await _build_implement_prompt(req, tmpdir)
        elif req.action == "fix-pr":
            prompt = await _build_fix_pr_prompt(req, tmpdir)
        elif req.action == "review":
            prompt = await _build_review_prompt(req, tmpdir)
        else:
            raise ValueError(f"Unknown action: {req.action}")

        timeout = 1800  # 30 min for implement/review
        return await run_codex(prompt, cwd=tmpdir, timeout=timeout)

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


async def _gh_output(*args) -> str:
    """Run gh command and return stdout."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return stdout.decode().strip()


async def _build_implement_prompt(req, tmpdir: str) -> str:
    issue_text = await _gh_output(
        "gh", "issue", "view", str(req.issue_number),
        "--repo", req.repo,
        "--json", "title,body",
        "--jq", '.title + "\\n\\n" + .body',
    )

    agents_md = ""
    agents_path = os.path.join(tmpdir, "AGENTS.md")
    if os.path.exists(agents_path):
        with open(agents_path) as f:
            agents_md = f.read()

    branch = f"issue-{req.issue_number}-impl"
    prompt = f"""You are working in a cloned repo at {tmpdir}.

ISSUE #{req.issue_number}:
{issue_text}
"""
    if agents_md:
        prompt += f"\nAGENTS.md (project conventions):\n{agents_md}\n"

    prompt += f"""
INSTRUCTIONS:
1. Create and checkout branch: {branch}
2. Read the codebase to understand the project structure.
3. Follow the Approach / steps described in the issue.
4. Implement all required changes.
5. Commit your changes with a descriptive message.
6. Push the branch: git push -u origin {branch}
7. Create a PR: gh pr create --repo {req.repo} --title "Implement #{req.issue_number}" --body "Closes #{req.issue_number}" --head {branch}

Work carefully and verify your changes before committing.
"""
    return prompt


async def _build_fix_pr_prompt(req, tmpdir: str) -> str:
    comments = await _gh_output(
        "gh", "pr", "view", str(req.pr_number),
        "--repo", req.repo, "--comments",
    )
    branch = await _gh_output(
        "gh", "pr", "view", str(req.pr_number),
        "--repo", req.repo,
        "--json", "headRefName", "--jq", ".headRefName",
    )
    proc = await asyncio.create_subprocess_exec(
        "git", "checkout", branch, cwd=tmpdir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()

    return f"""You are working in a cloned repo at {tmpdir}, on branch {branch}.

PR #{req.pr_number} REVIEW COMMENTS:
{comments}

INSTRUCTIONS:
1. Read the review comments carefully.
2. Fix all issues mentioned.
3. Commit fixes with a descriptive message.
4. Push: git push origin {branch}
"""


async def _build_review_prompt(req, tmpdir: str) -> str:
    agents_md = ""
    agents_path = os.path.join(tmpdir, "AGENTS.md")
    if os.path.exists(agents_path):
        with open(agents_path) as f:
            agents_md = f.read()

    prompt = f"""You are a strict code reviewer for repository {req.repo}.
You are working in a cloned repo at {tmpdir}.
"""
    if agents_md:
        prompt += f"\nAGENTS.md (project conventions):\n{agents_md}\n"

    prompt += f"""
Review PR #{req.pr_number}:
1. Run: gh pr diff {req.pr_number} --repo {req.repo}
2. Find the linked issue number from the PR body (Closes #N)
3. Read the issue: gh issue view N --repo {req.repo}
4. Check each Acceptance Criteria item
5. Check code quality: no dead code, no hardcoded values, proper error handling
6. Browser smoke test (if index.html exists):
   - python3 -m http.server 8000 &
   - Write and run a Playwright script checking for console errors
   - Kill server when done

Take exactly one action:
APPROVE: gh pr review {req.pr_number} --repo {req.repo} --approve -b 'Verified: [list]'
         gh pr merge {req.pr_number} --repo {req.repo} --squash --delete-branch
REQUEST CHANGES: gh pr review {req.pr_number} --repo {req.repo} --request-changes -b 'Issues: [list]'

You MUST run one of the above gh commands before finishing.
"""
    return prompt
```

- [ ] **Step 2: Verify syntax**

```bash
python3 -c "import ast; ast.parse(open('server/codex_runner.py').read()); print('OK')"
```

- [ ] **Step 3: Commit**

```bash
git add server/codex_runner.py
git commit -m "feat: Codex CLI runner with implement/fix-pr/review support"
```

---

### Task 7: Workflow templates for child repos

**Files:**
- Create: `templates/workflows/implement.yml` (copy from byok-tg-runner, adapt RUNNER_URL references)
- Create: `templates/workflows/review.yml` (copy from byok-tg-runner)

- [ ] **Step 1: Copy and adapt implement.yml**

Copy `/home/ct/copilot/byok-tg-runner/templates/workflows/implement.yml` to `templates/workflows/implement.yml`.

No changes needed — it already uses `RUNNER_URL` and `RUNNER_API_KEY` secrets which will be set on child repos by the setup process. The `RUNNER_URL` will point to the CF Worker `/implement` endpoint.

- [ ] **Step 2: Copy review.yml**

Copy `/home/ct/copilot/byok-tg-runner/templates/workflows/review.yml` to `templates/workflows/review.yml`.

No changes needed.

- [ ] **Step 3: Commit**

```bash
git add templates/
git commit -m "feat: add workflow templates for child repos (implement + review)"
```

---

## Chunk 2: Cloudflare Worker

### Task 8: Worker project setup

**Files:**
- Create: `worker/package.json`
- Create: `worker/wrangler.toml`
- Create: `worker/tsconfig.json`

- [ ] **Step 1: Create package.json**

Create `worker/package.json`:
```json
{
  "name": "tg-codex-bot-worker",
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

- [ ] **Step 2: Create wrangler.toml**

Create `worker/wrangler.toml`:
```toml
name = "tg-codex-bot-worker"
main = "src/index.ts"
compatibility_date = "2024-09-02"

[[kv_namespaces]]
binding = "BOT_KV"
id = "PLACEHOLDER_KV_ID"

# Secrets (set via wrangler secret put):
# OPENAI_API_KEY       - OpenAI API key for simple chat
# OPENAI_BASE_URL      - custom OpenAI endpoint (optional)
# TELEGRAM_BOT_TOKEN   - Telegram Bot API token
# TELEGRAM_SECRET      - webhook verification secret
# ALLOWED_CHAT_ID      - comma-separated Telegram chat IDs
# TASK_API_KEY         - shared key with task server
# GH_PAT              - GitHub PAT for Codespace API
```

- [ ] **Step 3: Create tsconfig.json**

Create `worker/tsconfig.json`:
```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "ES2022",
    "moduleResolution": "bundler",
    "lib": ["ES2022"],
    "types": ["@cloudflare/workers-types"],
    "strict": true
  }
}
```

- [ ] **Step 4: Install dependencies**

```bash
cd worker && npm install && cd ..
```

- [ ] **Step 5: Commit**

```bash
git add worker/package.json worker/wrangler.toml worker/tsconfig.json
git commit -m "feat: worker project setup (wrangler + typescript)"
```

---

### Task 9: Worker — types, helpers, KV memory

**Files:**
- Create: `worker/src/index.ts` (first part: types, KV helpers, Telegram helper)

- [ ] **Step 1: Create worker/src/index.ts with types and helpers**

Create `worker/src/index.ts`:
```typescript
export interface Env {
  BOT_KV: KVNamespace;
  OPENAI_API_KEY: string;
  OPENAI_BASE_URL: string;
  TELEGRAM_BOT_TOKEN: string;
  TELEGRAM_SECRET: string;
  ALLOWED_CHAT_ID: string;
  TASK_API_KEY: string;
  GH_PAT: string;
}

interface TelegramUpdate {
  message?: {
    chat: { id: number };
    from?: { id: number };
    text?: string;
  };
}

interface HistoryEntry {
  role: "user" | "bot";
  text: string;
  ts: number;
}

const MAX_HISTORY_PER_ROLE = 20;
const MAX_HISTORY_JSON_CHARS = 2000;
const CODESPACE_REPO = "yazelin/tg-codex-bot";

const corsHeaders: Record<string, string> = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, X-Api-Key, X-Secret, X-Telegram-Bot-Api-Secret-Token",
};

// ---------------------------------------------------------------------------
// KV helpers
// ---------------------------------------------------------------------------

async function appendHistory(
  kv: KVNamespace, chatId: string, role: "user" | "bot", text: string,
): Promise<void> {
  const key = `chat:${chatId}:${role}`;
  const raw = await kv.get(key);
  const entries: HistoryEntry[] = raw ? JSON.parse(raw) : [];
  entries.push({ role, text, ts: Date.now() });
  if (entries.length > MAX_HISTORY_PER_ROLE) {
    entries.splice(0, entries.length - MAX_HISTORY_PER_ROLE);
  }
  await kv.put(key, JSON.stringify(entries));
}

async function getHistory(kv: KVNamespace, chatId: string): Promise<HistoryEntry[]> {
  const [userRaw, botRaw] = await Promise.all([
    kv.get(`chat:${chatId}:user`),
    kv.get(`chat:${chatId}:bot`),
  ]);
  const userEntries: HistoryEntry[] = userRaw ? JSON.parse(userRaw) : [];
  const botEntries: HistoryEntry[] = botRaw ? JSON.parse(botRaw) : [];
  const merged = [...userEntries, ...botEntries];
  merged.sort((a, b) => a.ts - b.ts);
  return merged;
}

function truncateHistory(history: HistoryEntry[]): string {
  let json = JSON.stringify(history);
  while (json.length > MAX_HISTORY_JSON_CHARS && history.length > 0) {
    history.shift();
    json = JSON.stringify(history);
  }
  return json;
}

async function getPrefs(kv: KVNamespace, chatId: string): Promise<Record<string, string>> {
  const raw = await kv.get(`chat:${chatId}:prefs`);
  return raw ? JSON.parse(raw) : {};
}

async function setPrefs(
  kv: KVNamespace, chatId: string, prefs: Record<string, string>,
): Promise<void> {
  const existing = await getPrefs(kv, chatId);
  Object.assign(existing, prefs);
  await kv.put(`chat:${chatId}:prefs`, JSON.stringify(existing));
}

async function incrementStats(kv: KVNamespace, ...counters: string[]): Promise<void> {
  for (const counter of counters) {
    const key = `stats:${counter}`;
    const raw = await kv.get(key);
    const val = raw ? parseInt(raw, 10) : 0;
    await kv.put(key, String(val + 1));
  }
}

// ---------------------------------------------------------------------------
// Telegram helper
// ---------------------------------------------------------------------------

async function sendTelegram(token: string, chatId: string, text: string): Promise<void> {
  for (let i = 0; i < text.length; i += 4096) {
    await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_id: chatId, text: text.substring(i, i + 4096) }),
    });
  }
}

function jsonResponse(data: unknown, status = 200): Response {
  return Response.json(data, { status, headers: corsHeaders });
}
```

- [ ] **Step 2: Commit (partial — will add main handler in next tasks)**

```bash
git add worker/src/index.ts
git commit -m "feat: worker types, KV helpers, Telegram helper"
```

---

### Task 10: Worker — OpenAI simple chat

**Files:**
- Modify: `worker/src/index.ts` (add OpenAI chat function)

- [ ] **Step 1: Add OpenAI chat function**

Append to `worker/src/index.ts` after the `jsonResponse` function:

```typescript
// ---------------------------------------------------------------------------
// OpenAI simple chat
// ---------------------------------------------------------------------------

const CHAT_SYSTEM_PROMPT = `You are a helpful, friendly AI assistant responding to Telegram messages.
Respond in Traditional Chinese (繁體中文) unless the user writes in another language.
Keep responses under 4096 characters.

IMPORTANT: If the user's request requires ANY of the following, respond with ONLY the text <<<ROUTE_TO_CODEX>>> and nothing else:
- Creating or modifying GitHub repositories
- Writing, editing, or reviewing code
- Running shell commands or build tools
- Multi-step implementation tasks
- Creating GitHub issues or PRs
- Web research requiring multiple sources
- Any task that needs file system access`;

async function openaiChat(
  env: Env, userMessage: string, history: HistoryEntry[], prefs: Record<string, string>,
): Promise<string> {
  const baseUrl = env.OPENAI_BASE_URL || "https://api.openai.com/v1";

  const messages: Array<{ role: string; content: string }> = [
    { role: "system", content: CHAT_SYSTEM_PROMPT },
  ];

  // Add preferences context if any
  if (Object.keys(prefs).length > 0) {
    messages.push({
      role: "system",
      content: `User preferences: ${JSON.stringify(prefs)}`,
    });
  }

  // Add recent history (last 10 exchanges)
  const recent = history.slice(-20);
  for (const entry of recent) {
    messages.push({
      role: entry.role === "user" ? "user" : "assistant",
      content: entry.text,
    });
  }

  messages.push({ role: "user", content: userMessage });

  const res = await fetch(`${baseUrl}/chat/completions`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${env.OPENAI_API_KEY}`,
    },
    body: JSON.stringify({
      model: "gpt-5.3-codex",
      messages,
      max_tokens: 2048,
      temperature: 0.7,
    }),
  });

  if (!res.ok) {
    const err = await res.text();
    throw new Error(`OpenAI API error ${res.status}: ${err.substring(0, 200)}`);
  }

  const data = (await res.json()) as { choices: Array<{ message: { content: string } }> };
  return data.choices?.[0]?.message?.content?.trim() || "(no response)";
}
```

- [ ] **Step 2: Commit**

```bash
git add worker/src/index.ts
git commit -m "feat: worker OpenAI simple chat with routing marker"
```

---

### Task 11: Worker — Codespace lifecycle management

**Files:**
- Modify: `worker/src/index.ts` (add Codespace management functions)

- [ ] **Step 1: Add Codespace management**

Append to `worker/src/index.ts`:

```typescript
// ---------------------------------------------------------------------------
// GitHub Codespace management
// ---------------------------------------------------------------------------

interface CodespaceInfo {
  name: string;
  state: string;
}

async function ghApi(pat: string, path: string, method = "GET", body?: unknown): Promise<Response> {
  const opts: RequestInit = {
    method,
    headers: {
      Authorization: `Bearer ${pat}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
    },
  };
  if (body) {
    opts.headers = { ...opts.headers as Record<string, string>, "Content-Type": "application/json" };
    opts.body = JSON.stringify(body);
  }
  return fetch(`https://api.github.com${path}`, opts);
}

async function findCodespace(pat: string): Promise<CodespaceInfo | null> {
  const res = await ghApi(pat, "/user/codespaces");
  if (!res.ok) return null;
  const data = (await res.json()) as { codespaces: Array<{ name: string; state: string; repository: { full_name: string } }> };
  const cs = data.codespaces.find((c) => c.repository.full_name === CODESPACE_REPO);
  return cs ? { name: cs.name, state: cs.state } : null;
}

async function createCodespace(pat: string): Promise<string> {
  const res = await ghApi(pat, "/user/codespaces", "POST", {
    repository_id: await getRepoId(pat),
    ref: "main",
    machine: "basicLinux32gb",
  });
  if (!res.ok) {
    const err = await res.text();
    throw new Error(`Failed to create codespace: ${err.substring(0, 200)}`);
  }
  const data = (await res.json()) as { name: string };
  return data.name;
}

async function getRepoId(pat: string): Promise<number> {
  const res = await ghApi(pat, `/repos/${CODESPACE_REPO}`);
  const data = (await res.json()) as { id: number };
  return data.id;
}

async function startCodespace(pat: string, name: string): Promise<void> {
  const res = await ghApi(pat, `/user/codespaces/${name}/start`, "POST");
  if (!res.ok && res.status !== 409) {
    // 409 = already running, that's fine
    const err = await res.text();
    throw new Error(`Failed to start codespace: ${err.substring(0, 200)}`);
  }
}

async function stopCodespace(pat: string, name: string): Promise<void> {
  await ghApi(pat, `/user/codespaces/${name}/stop`, "POST");
}

async function waitForCodespace(pat: string, name: string, timeoutMs = 120000): Promise<void> {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const res = await ghApi(pat, `/user/codespaces/${name}`);
    const data = (await res.json()) as { state: string };
    if (data.state === "Available") return;
    if (data.state === "Failed") throw new Error("Codespace failed to start");
    await new Promise((r) => setTimeout(r, 5000));
  }
  throw new Error(`Codespace not ready after ${timeoutMs / 1000}s`);
}

async function getCodespacePortUrl(pat: string, name: string): Promise<string> {
  // Codespace port forwarding URL follows a pattern
  // Try the ports API first
  const res = await ghApi(pat, `/user/codespaces/${name}`);
  const data = (await res.json()) as {
    name: string;
    web_url: string;
    runtime_constraints?: { allowed_port_privacy_settings: string[] };
  };

  // Port forwarding URL format: https://{name}-{port}.app.github.dev
  return `https://${name}-8080.app.github.dev`;
}

async function ensureCodespaceReady(pat: string): Promise<{ name: string; taskUrl: string }> {
  let cs = await findCodespace(pat);

  if (!cs) {
    // Create new codespace
    const name = await createCodespace(pat);
    await waitForCodespace(pat, name);
    const taskUrl = await getCodespacePortUrl(pat, name);
    return { name, taskUrl };
  }

  if (cs.state === "Shutdown" || cs.state === "Stopped") {
    await startCodespace(pat, cs.name);
    await waitForCodespace(pat, cs.name);
  } else if (cs.state !== "Available") {
    await waitForCodespace(pat, cs.name);
  }

  const taskUrl = await getCodespacePortUrl(pat, cs.name);
  return { name: cs.name, taskUrl };
}
```

- [ ] **Step 2: Commit**

```bash
git add worker/src/index.ts
git commit -m "feat: worker Codespace lifecycle (find, create, start, stop, wait)"
```

---

### Task 12: Worker — main handler and Telegram message routing

**Files:**
- Modify: `worker/src/index.ts` (add main fetch handler and message routing)

- [ ] **Step 1: Add main handler**

Append to `worker/src/index.ts`:

```typescript
// ---------------------------------------------------------------------------
// Main handler
// ---------------------------------------------------------------------------

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders });
    }

    // --- GET /status ---
    if (request.method === "GET" && url.pathname === "/status") {
      try {
        const cs = await findCodespace(env.GH_PAT);
        return jsonResponse({
          status: "ok",
          codespace: cs ? { name: cs.name, state: cs.state } : null,
        });
      } catch {
        return jsonResponse({ status: "ok", codespace: null });
      }
    }

    // --- POST /implement --- proxy to Codespace
    if (request.method === "POST" && url.pathname === "/implement") {
      const apiKey = request.headers.get("x-api-key");
      if (apiKey !== env.TASK_API_KEY) {
        return new Response("Unauthorized", { status: 401 });
      }
      const body = await request.json();
      ctx.waitUntil(handleImplement(env, body as Record<string, unknown>));
      return jsonResponse({ status: "accepted" });
    }

    // --- POST /api/callback --- from task server
    if (request.method === "POST" && url.pathname === "/api/callback") {
      const secret = request.headers.get("X-Secret");
      if (secret !== env.TASK_API_KEY) {
        return new Response("Unauthorized", { status: 401 });
      }
      const payload = (await request.json()) as Record<string, unknown>;
      if (payload.type === "bot_reply") {
        const chatId = String(payload.chat_id);
        const text = String(payload.text ?? "");
        if (chatId && text) {
          await appendHistory(env.BOT_KV, chatId, "bot", text);
        }
      }
      return jsonResponse({ ok: true });
    }

    // --- GET /api/history/:chatId ---
    if (request.method === "GET" && url.pathname.startsWith("/api/history/")) {
      const chatId = url.pathname.split("/api/history/")[1];
      if (!chatId) return jsonResponse({ error: "missing chatId" }, 400);
      const history = await getHistory(env.BOT_KV, chatId);
      return jsonResponse({ chat_id: chatId, history });
    }

    // --- GET /api/stats ---
    if (request.method === "GET" && url.pathname === "/api/stats") {
      const [totalMessages, totalApps, totalBuilds] = await Promise.all([
        env.BOT_KV.get("stats:totalMessages"),
        env.BOT_KV.get("stats:totalApps"),
        env.BOT_KV.get("stats:totalBuilds"),
      ]);
      return jsonResponse({
        totalMessages: parseInt(totalMessages ?? "0", 10),
        totalApps: parseInt(totalApps ?? "0", 10),
        totalBuilds: parseInt(totalBuilds ?? "0", 10),
      });
    }

    // --- POST (webhook) --- Telegram
    if (request.method !== "POST") {
      return new Response("Method Not Allowed", { status: 405 });
    }

    // Verify Telegram webhook secret
    const telegramSecret = request.headers.get("X-Telegram-Bot-Api-Secret-Token");
    if (telegramSecret !== env.TELEGRAM_SECRET) {
      return new Response("Forbidden", { status: 403 });
    }

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

    const chatId = String(message.chat.id);
    const allowed = env.ALLOWED_CHAT_ID.split(",").map((s) => s.trim());
    if (!allowed.includes(chatId)) {
      return new Response("OK", { status: 200 });
    }

    const text = message.text.trim();
    ctx.waitUntil(handleTelegramMessage(env, chatId, text));
    return new Response("OK", { status: 200 });
  },
} satisfies ExportedHandler<Env>;

// ---------------------------------------------------------------------------
// Telegram message handler
// ---------------------------------------------------------------------------

async function handleTelegramMessage(env: Env, chatId: string, text: string): Promise<void> {
  // --- /reset ---
  if (text === "/reset") {
    await Promise.all([
      env.BOT_KV.delete(`chat:${chatId}:user`),
      env.BOT_KV.delete(`chat:${chatId}:bot`),
      env.BOT_KV.delete(`chat:${chatId}:prefs`),
    ]);
    await sendTelegram(env.TELEGRAM_BOT_TOKEN, chatId, "記憶已清除，可以重新開始");
    return;
  }

  // --- /status ---
  if (text === "/status") {
    try {
      const cs = await findCodespace(env.GH_PAT);
      const msg = cs
        ? `Codespace: ${cs.name}\n狀態: ${cs.state}`
        : "沒有找到 Codespace";
      await sendTelegram(env.TELEGRAM_BOT_TOKEN, chatId, msg);
    } catch (err) {
      await sendTelegram(env.TELEGRAM_BOT_TOKEN, chatId, `查詢失敗: ${err}`);
    }
    return;
  }

  // --- /setpref ---
  const prefMatch = text.match(/^\/setpref\s+(\w+)\s+(.+)$/);
  if (prefMatch) {
    await setPrefs(env.BOT_KV, chatId, { [prefMatch[1]]: prefMatch[2] });
    await sendTelegram(env.TELEGRAM_BOT_TOKEN, chatId, `已設定 ${prefMatch[1]} = ${prefMatch[2]}`);
    return;
  }

  // Store user message
  await appendHistory(env.BOT_KV, chatId, "user", text);
  await incrementStats(env.BOT_KV, "totalMessages");

  // --- /build owner/repo ---
  const buildMatch = text.match(/^\/build\s+(\S+)$/);
  if (buildMatch) {
    await incrementStats(env.BOT_KV, "totalBuilds");
    try {
      const res = await ghApi(env.GH_PAT, `/repos/${buildMatch[1]}/actions/workflows/implement.yml/dispatches`, "POST", {
        ref: "main",
      });
      if (res.ok || res.status === 204) {
        await sendTelegram(env.TELEGRAM_BOT_TOKEN, chatId, `🚀 已觸發 ${buildMatch[1]} 開發流程`);
      } else {
        const err = await res.text();
        await sendTelegram(env.TELEGRAM_BOT_TOKEN, chatId, `❌ 觸發失敗: ${err.substring(0, 200)}`);
      }
    } catch (err) {
      await sendTelegram(env.TELEGRAM_BOT_TOKEN, chatId, `❌ Build error: ${err}`);
    }
    return;
  }

  // --- /msg owner/repo#N message ---
  const msgMatch = text.match(/^\/msg\s+(\S+)#(\d+)\s+([\s\S]+)$/);
  if (msgMatch) {
    const [, repo, num, msgText] = msgMatch;
    try {
      await ghApi(env.GH_PAT, `/repos/${repo}/issues/${num}/comments`, "POST", {
        body: `📝 User instruction:\n\n${msgText}`,
      });
      // Trigger implement workflow
      await ghApi(env.GH_PAT, `/repos/${repo}/actions/workflows/implement.yml/dispatches`, "POST", {
        ref: "main",
      });
      await sendTelegram(env.TELEGRAM_BOT_TOKEN, chatId, `📝 已將指示傳達給 ${repo} #${num}`);
    } catch (err) {
      await sendTelegram(env.TELEGRAM_BOT_TOKEN, chatId, `❌ Message error: ${err}`);
    }
    return;
  }

  // --- Determine if Codex path ---
  const isCodexCommand =
    text.startsWith("/app ") || text === "/app" ||
    text.startsWith("/issue ") || text === "/issue" ||
    text.startsWith("/research ") || text === "/research";

  if (isCodexCommand) {
    let command = "chat";
    if (text.startsWith("/app")) { command = "app"; await incrementStats(env.BOT_KV, "totalApps"); }
    else if (text.startsWith("/issue")) command = "issue";
    else if (text.startsWith("/research")) command = "research";

    await dispatchToCodespace(env, chatId, text, command);
    return;
  }

  // --- Simple chat via OpenAI API ---
  try {
    const history = await getHistory(env.BOT_KV, chatId);
    const prefs = await getPrefs(env.BOT_KV, chatId);
    const reply = await openaiChat(env, text, history, prefs);

    // Check for routing marker
    if (reply.includes("<<<ROUTE_TO_CODEX>>>")) {
      await dispatchToCodespace(env, chatId, text, "chat");
      return;
    }

    await sendTelegram(env.TELEGRAM_BOT_TOKEN, chatId, reply);
    await appendHistory(env.BOT_KV, chatId, "bot", reply);
  } catch (err) {
    console.error("Chat error:", err);
    await sendTelegram(env.TELEGRAM_BOT_TOKEN, chatId, `Error: ${err}`);
  }
}

// ---------------------------------------------------------------------------
// Dispatch to Codespace
// ---------------------------------------------------------------------------

async function dispatchToCodespace(
  env: Env, chatId: string, text: string, command: string,
): Promise<void> {
  await sendTelegram(env.TELEGRAM_BOT_TOKEN, chatId, "⏳ 處理中...");

  try {
    const { name, taskUrl } = await ensureCodespaceReady(env.GH_PAT);

    // Wait a few seconds for task server to be ready after Codespace start
    await new Promise((r) => setTimeout(r, 3000));

    // Build prompt with history
    const history = await getHistory(env.BOT_KV, chatId);
    const truncated = truncateHistory([...history]);
    let prompt = text;
    if (history.length > 0) {
      const lines = history.slice(-20).map((e) => `[${e.role}]: ${e.text}`);
      prompt = `--- Chat History ---\n${lines.join("\n")}\n--- End History ---\n\n${text}`;
    }

    // Send task to Codespace
    const timeout = command === "app" ? 900000 : command === "implement" ? 1800000 : 300000;
    const res = await fetch(`${taskUrl}/task`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Api-Key": env.TASK_API_KEY,
      },
      body: JSON.stringify({ prompt, command, chat_id: chatId }),
      signal: AbortSignal.timeout(timeout),
    });

    if (!res.ok) {
      const err = await res.text();
      throw new Error(`Task server error ${res.status}: ${err.substring(0, 200)}`);
    }

    const result = (await res.json()) as { status: string; reply: string; error: string };
    const reply = result.status === "ok" ? result.reply : `Error: ${result.error}`;

    await sendTelegram(env.TELEGRAM_BOT_TOKEN, chatId, reply);
    await appendHistory(env.BOT_KV, chatId, "bot", reply);

    // Stop Codespace immediately
    await stopCodespace(env.GH_PAT, name);
  } catch (err) {
    console.error("Codespace dispatch error:", err);
    await sendTelegram(env.TELEGRAM_BOT_TOKEN, chatId, `❌ Codex error: ${err}`);
  }
}

// ---------------------------------------------------------------------------
// Handle /implement (from child repo workflows)
// ---------------------------------------------------------------------------

async function handleImplement(env: Env, body: Record<string, unknown>): Promise<void> {
  const chatId = String(body.notify_chat_id || "");

  try {
    const { name, taskUrl } = await ensureCodespaceReady(env.GH_PAT);
    await new Promise((r) => setTimeout(r, 3000));

    const res = await fetch(`${taskUrl}/task`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Api-Key": env.TASK_API_KEY,
      },
      body: JSON.stringify({
        prompt: "",
        command: "implement",
        chat_id: chatId,
        repo: body.repo,
        action: body.action,
        issue_number: body.issue_number || 0,
        pr_number: body.pr_number || 0,
        notify_chat_id: chatId,
      }),
      signal: AbortSignal.timeout(1800000), // 30 min
    });

    const result = (await res.json()) as { status: string; reply: string; error: string };

    if (chatId) {
      const msg = result.status === "ok"
        ? `✅ ${body.action} done for ${body.repo}`
        : `❌ ${body.action} failed: ${result.error}`;
      await sendTelegram(env.TELEGRAM_BOT_TOKEN, chatId, msg);
    }

    await stopCodespace(env.GH_PAT, name);
  } catch (err) {
    if (chatId) {
      await sendTelegram(env.TELEGRAM_BOT_TOKEN, chatId, `❌ Implement error: ${err}`);
    }
  }
}
```

- [ ] **Step 2: Verify TypeScript compiles**

```bash
cd worker && npx tsc --noEmit && cd ..
```

- [ ] **Step 3: Commit**

```bash
git add worker/src/index.ts
git commit -m "feat: worker main handler — routing, OpenAI chat, Codespace dispatch"
```

---

## Chunk 3: Setup Scripts + Final Assembly

### Task 13: sync-secrets.sh

**Files:**
- Create: `scripts/sync-secrets.sh`

- [ ] **Step 1: Create sync-secrets.sh**

Create `scripts/sync-secrets.sh`:
```bash
#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_SLUG=$(cd "$REPO_ROOT" && git remote get-url origin | sed 's|.*github.com[:/]||;s|\.git$||')

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
RESET='\033[0m'

success() { echo -e "  ${GREEN}✓${RESET} $*"; }

MODE="${1:-sync}"

sync_secret() {
  local name="$1"
  local value="$2"
  gh secret set "$name" --repo "$REPO_SLUG" --body "$value" 2>/dev/null || true
  gh codespace secret set "$name" --repo "$REPO_SLUG" --body "$value" 2>/dev/null || true
  echo "$value" | (cd "$REPO_ROOT/worker" && npx wrangler secret put "$name" 2>&1 | grep -v "WARNING\|update available") || true
  success "$name → GitHub + Codespace + Cloudflare"
}

if [[ "$MODE" == "--check" ]]; then
  echo -e "${BOLD}Checking secret existence...${RESET}\n"
  GH_SECRETS=$(gh secret list --repo "$REPO_SLUG" --json name -q '.[].name' | sort)
  echo "GitHub Secrets: $(echo "$GH_SECRETS" | tr '\n' ' ')"
  CS_SECRETS=$(gh codespace secret list --repo "$REPO_SLUG" --json name -q '.[].name' 2>/dev/null | sort)
  echo "Codespace Secrets: $(echo "$CS_SECRETS" | tr '\n' ' ')"
  exit 0
fi

echo -e "${BOLD}Syncing shared secrets...${RESET}\n"

NEW_TASK_KEY=$(openssl rand -hex 32)
echo -e "${YELLOW}Regenerating TASK_API_KEY...${RESET}"
sync_secret "TASK_API_KEY" "$NEW_TASK_KEY"

echo -e "\n${GREEN}${BOLD}Done.${RESET} Restart Codespace to pick up new secrets."
```

```bash
chmod +x scripts/sync-secrets.sh
```

- [ ] **Step 2: Commit**

```bash
git add scripts/sync-secrets.sh
git commit -m "feat: add sync-secrets script for shared secret management"
```

---

### Task 14: setup.sh — one-click installation

**Files:**
- Create: `scripts/setup.sh`

- [ ] **Step 1: Create setup.sh**

Create `scripts/setup.sh`:
```bash
#!/usr/bin/env bash
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

info()    { echo -e "${CYAN}ℹ ${RESET}$*"; }
success() { echo -e "${GREEN}✓ ${RESET}$*"; }
warn()    { echo -e "${YELLOW}⚠ ${RESET}$*"; }
error()   { echo -e "${RED}✗ ${RESET}$*" >&2; exit 1; }
step()    { echo -e "\n${BOLD}${CYAN}══ $* ${RESET}"; }

ask() {
  local var="$1" msg="$2" secret="${3:-false}" default="${4:-}"
  echo ""
  if [[ -n "$default" ]]; then
    echo -e "${YELLOW}▶ ${RESET}$msg (預設: $default)"
  else
    echo -e "${YELLOW}▶ ${RESET}$msg"
  fi
  if [[ "$secret" == "true" ]]; then
    read -rs value; echo ""
  else
    read -r value
  fi
  value="${value:-$default}"
  [[ -z "$value" ]] && error "不能為空"
  eval "$var=\"\$value\""
}

# ── 0. Welcome ──
clear
echo -e "${BOLD}"
cat << 'BANNER'
  _                         _                _           _
 | |_ __ _        ___ ___  __| | _____  __   | |__   ___ | |_
 | __/ _` |_____ / __/ _ \ / _` |/ _ \ \/ /___| '_ \ / _ \| __|
 | || (_| |_____| (_| (_) | (_| |  __/>  <____| |_) | (_) | |_
  \__\__, |      \___\___/ \__,_|\___/_/\_\   |_.__/ \___/ \__|
     |___/
BANNER
echo -e "${RESET}"
echo -e "  Telegram Bot + Cloudflare Worker + GitHub Codespace + Codex CLI"
echo -e "  一鍵安裝約需 ${BOLD}5 分鐘${RESET}\n"

# ── 1. Prerequisites ──
step "步驟 1/8：檢查前置工具"
for tool in gh node npx openssl; do
  command -v $tool &>/dev/null && success "$tool" || error "$tool 未安裝"
done
gh auth status &>/dev/null || error "請先執行 gh auth login"
success "GitHub CLI 已登入"

# ── 2. Repo ──
step "步驟 2/8：確認 Repo"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
REPO_SLUG=$(git remote get-url origin 2>/dev/null | sed 's|.*github.com[:/]||;s|\.git$||' || true)
if [[ -z "$REPO_SLUG" ]]; then
  error "找不到 git remote，請在 tg-codex-bot repo 根目錄執行"
fi
success "Repo: $REPO_SLUG"

# ── 3. Collect secrets ──
step "步驟 3/8：收集必要資訊"

echo -e "\n${BOLD}[Telegram Bot]${RESET}"
ask TELEGRAM_BOT_TOKEN "Bot Token (from @BotFather):" true
ask TELEGRAM_CHAT_ID "Chat ID (from @userinfobot):"

echo -e "\n${BOLD}[OpenAI API]${RESET}"
ask OPENAI_API_KEY "API Key:" true
ask OPENAI_BASE_URL "API Base URL:" false "https://api.openai.com/v1"

echo -e "\n${BOLD}[GitHub PAT]${RESET}"
info "需要 scopes: codespace, repo, workflow"
ask GH_PAT "GitHub PAT:" true

echo -e "\n${BOLD}[Cloudflare]${RESET}"
ask CF_API_TOKEN "API Token:" true
# Detect account ID
CF_ACCOUNT_ID=$(cd worker && npx wrangler whoami 2>&1 | grep -oE '[0-9a-f]{32}' | head -1 || true)
if [[ -z "$CF_ACCOUNT_ID" ]]; then
  ask CF_ACCOUNT_ID "Account ID:"
else
  success "Account ID: $CF_ACCOUNT_ID"
fi

echo -e "\n${BOLD}[App Factory]${RESET}"
ask APPS_ORG "子 repo 的 GitHub Organization:" false "aw-apps"

# Auto-generate secrets
TASK_API_KEY=$(openssl rand -hex 32)
TELEGRAM_SECRET=$(openssl rand -hex 16)
success "已自動生成 TASK_API_KEY 和 TELEGRAM_SECRET"

# ── 4. KV Namespace ──
step "步驟 4/8：Cloudflare KV"
cd worker
if [[ ! -d node_modules ]]; then
  npm install --silent
fi

EXISTING_KV_ID=$(npx wrangler kv namespace list 2>&1 | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
    for ns in data:
        if ns.get('title','').endswith('-BOT_KV'):
            print(ns['id']); break
except: pass
" 2>/dev/null || true)

if [[ -n "$EXISTING_KV_ID" ]]; then
  KV_NAMESPACE_ID="$EXISTING_KV_ID"
  success "KV 已存在: $KV_NAMESPACE_ID"
else
  KV_OUTPUT=$(npx wrangler kv namespace create "BOT_KV" 2>&1 || true)
  KV_NAMESPACE_ID=$(echo "$KV_OUTPUT" | grep -oE '[0-9a-f]{32}' | tail -1)
  success "KV 已建立: $KV_NAMESPACE_ID"
fi

sed -i "s|id = \"PLACEHOLDER_KV_ID\"|id = \"$KV_NAMESPACE_ID\"|" wrangler.toml
cd "$REPO_ROOT"

# ── 5. Set Codespace Secrets ──
step "步驟 5/8：Codespace Secrets"
for name_val in \
  "OPENAI_API_KEY:$OPENAI_API_KEY" \
  "OPENAI_BASE_URL:$OPENAI_BASE_URL" \
  "TASK_API_KEY:$TASK_API_KEY" \
  "GH_PAT:$GH_PAT" \
  "APPS_ORG:$APPS_ORG"; do
  IFS=: read -r name val <<< "$name_val"
  gh codespace secret set "$name" --repo "$REPO_SLUG" --body "$val"
  success "Codespace Secret: $name"
done

# ── 6. Set GitHub + Worker Secrets ──
step "步驟 6/8：GitHub & Worker Secrets"

# GitHub repo secrets
gh secret set CF_ACCOUNT_ID --repo "$REPO_SLUG" --body "$CF_ACCOUNT_ID"
gh secret set CF_API_TOKEN --repo "$REPO_SLUG" --body "$CF_API_TOKEN"
success "GitHub Secrets 已設定"

# Worker secrets
cd worker
for name_val in \
  "OPENAI_API_KEY:$OPENAI_API_KEY" \
  "OPENAI_BASE_URL:$OPENAI_BASE_URL" \
  "TELEGRAM_BOT_TOKEN:$TELEGRAM_BOT_TOKEN" \
  "TELEGRAM_SECRET:$TELEGRAM_SECRET" \
  "ALLOWED_CHAT_ID:$TELEGRAM_CHAT_ID" \
  "TASK_API_KEY:$TASK_API_KEY" \
  "GH_PAT:$GH_PAT"; do
  IFS=: read -r name val <<< "$name_val"
  echo "$val" | npx wrangler secret put "$name" 2>&1 | grep -v "WARNING\|update available"
  success "Worker Secret: $name"
done
cd "$REPO_ROOT"

# ── 7. Deploy Worker + Webhook ──
step "步驟 7/8：部署 Worker & 註冊 Webhook"

cd worker
DEPLOY_OUTPUT=$(npx wrangler deploy 2>&1)
echo "$DEPLOY_OUTPUT" | grep -v "WARNING\|update available"
WORKER_URL=$(echo "$DEPLOY_OUTPUT" | grep -oE 'https://[a-z0-9-]+\.[a-z0-9]+\.workers\.dev' | head -1)
cd "$REPO_ROOT"

if [[ -z "$WORKER_URL" ]]; then
  ask WORKER_URL "請手動輸入 Worker URL:"
fi
success "Worker 已部署: $WORKER_URL"

WEBHOOK_RESP=$(curl -sf "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook?url=${WORKER_URL}&secret_token=${TELEGRAM_SECRET}")
if echo "$WEBHOOK_RESP" | grep -q '"ok":true'; then
  success "Telegram Webhook 已設定"
else
  warn "Webhook 回應: $WEBHOOK_RESP"
fi

# ── 8. Create Codespace ──
step "步驟 8/8：建立 Codespace"

# Commit any wrangler.toml changes
git add worker/wrangler.toml 2>/dev/null || true
git diff --cached --quiet 2>/dev/null || git commit -m "chore: update KV namespace ID [setup]" 2>/dev/null || true
git push 2>/dev/null || true

EXISTING_CS=$(gh codespace list --repo "$REPO_SLUG" --json name -q '.[0].name' 2>/dev/null || true)
if [[ -n "$EXISTING_CS" ]]; then
  success "Codespace 已存在: $EXISTING_CS"
else
  info "建立 Codespace（首次約需 1-2 分鐘）..."
  gh codespace create --repo "$REPO_SLUG" --machine basicLinux32gb
  CS_NAME=$(gh codespace list --repo "$REPO_SLUG" --json name -q '.[0].name')
  success "Codespace 已建立: $CS_NAME"
  info "停止 Codespace（節省時數）..."
  gh codespace stop --codespace "$CS_NAME" 2>/dev/null || true
fi

# ── Done ──
echo ""
echo -e "${GREEN}${BOLD}══════════════════════════════════════════${RESET}"
echo -e "${GREEN}${BOLD}  安裝完成！${RESET}"
echo -e "${GREEN}${BOLD}══════════════════════════════════════════${RESET}"
echo ""
echo -e "  ${BOLD}Repo${RESET}        https://github.com/$REPO_SLUG"
echo -e "  ${BOLD}Worker${RESET}      $WORKER_URL"
echo -e "  ${BOLD}Bot${RESET}         https://t.me/$(curl -sf "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe" | grep -oP '(?<="username":")[^"]+')"
echo ""
echo -e "  ${CYAN}驗證：傳一條訊息給你的 Telegram bot${RESET}"
echo ""
```

```bash
chmod +x scripts/setup.sh
```

- [ ] **Step 2: Commit**

```bash
git add scripts/setup.sh
git commit -m "feat: one-click setup.sh — secrets, Worker deploy, webhook, Codespace"
```

---

### Task 15: README

**Files:**
- Create: `README.md`

- [ ] **Step 1: Create README.md**

Create `README.md` with project overview, architecture diagram (from spec), setup instructions (`./scripts/setup.sh`), command reference table, troubleshooting section. Keep it concise — the spec has the detailed design.

Key sections:
- Overview (3 sentences)
- Architecture diagram (from spec)
- Quick Start (`git clone` + `./scripts/setup.sh`)
- Commands table
- Secrets table
- Troubleshooting (common issues)

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: add README with setup guide and command reference"
```

---

### Task 16: Final assembly and push

- [ ] **Step 1: Verify all files exist**

```bash
ls -la .devcontainer/devcontainer.json .devcontainer/post-start.sh
ls -la worker/src/index.ts worker/wrangler.toml worker/package.json
ls -la server/main.py server/codex_runner.py server/requirements.txt
ls -la prompts/system.md config/codex-config.yaml
ls -la templates/workflows/implement.yml templates/workflows/review.yml
ls -la scripts/setup.sh scripts/sync-secrets.sh
ls -la AGENTS.md README.md .gitignore
```

- [ ] **Step 2: Verify Worker compiles**

```bash
cd worker && npx tsc --noEmit && cd ..
```

- [ ] **Step 3: Verify Python syntax**

```bash
python3 -c "import ast; ast.parse(open('server/main.py').read()); print('main.py OK')"
python3 -c "import ast; ast.parse(open('server/codex_runner.py').read()); print('codex_runner.py OK')"
```

- [ ] **Step 4: Push everything**

```bash
git push
```

- [ ] **Step 5: Run setup.sh to deploy**

```bash
./scripts/setup.sh
```

---

### Task 17: setup-child-repo.sh — helper script for child repo secrets & workflows

**Files:**
- Create: `scripts/setup-child-repo.sh`

- [ ] **Step 1: Create setup-child-repo.sh**

Create `scripts/setup-child-repo.sh`:
```bash
#!/usr/bin/env bash
set -euo pipefail

# Usage: ./scripts/setup-child-repo.sh <owner/repo>
# Sets up a child repo with required secrets and workflow files for the implement pipeline.

GREEN='\033[0;32m'
RED='\033[0;31m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

success() { echo -e "  ${GREEN}✓${RESET} $*"; }
error()   { echo -e "${RED}✗ ${RESET}$*" >&2; exit 1; }
info()    { echo -e "${CYAN}ℹ ${RESET}$*"; }

CHILD_REPO="${1:-}"
[[ -z "$CHILD_REPO" ]] && error "Usage: $0 <owner/repo>"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PARENT_SLUG=$(cd "$REPO_ROOT" && git remote get-url origin | sed 's|.*github.com[:/]||;s|\.git$||')

echo -e "\n${BOLD}Setting up child repo: ${CHILD_REPO}${RESET}\n"

# Verify child repo exists
gh repo view "$CHILD_REPO" &>/dev/null || error "Repo $CHILD_REPO not found or not accessible"

# ── 1. Get parent repo secrets ──
info "Reading parent secrets..."

# We need TASK_API_KEY and the Worker URL from parent config
# TASK_API_KEY: read from wrangler (cannot read secret values, so prompt if needed)
WORKER_URL=$(cd "$REPO_ROOT/worker" && grep -oP 'name\s*=\s*"\K[^"]+' wrangler.toml | head -1 || true)
if [[ -n "$WORKER_URL" ]]; then
  WORKER_URL="https://${WORKER_URL}.workers.dev"
else
  read -rp "Worker URL: " WORKER_URL
fi
success "Worker URL: $WORKER_URL"

# TASK_API_KEY cannot be read back from wrangler secrets
if [[ -z "${TASK_API_KEY:-}" ]]; then
  echo -e "\n${CYAN}TASK_API_KEY 無法自動讀取，請輸入（與 Worker 相同的值）:${RESET}"
  read -rs TASK_API_KEY; echo ""
fi
[[ -z "$TASK_API_KEY" ]] && error "TASK_API_KEY is required"

# Telegram chat ID for notifications
if [[ -z "${TELEGRAM_CHAT_ID:-}" ]]; then
  read -rp "Telegram Chat ID (for build notifications): " TELEGRAM_CHAT_ID
fi

# ── 2. Set child repo secrets ──
info "Setting child repo secrets..."

gh secret set RUNNER_URL    --repo "$CHILD_REPO" --body "$WORKER_URL"
success "RUNNER_URL"

gh secret set RUNNER_API_KEY --repo "$CHILD_REPO" --body "$TASK_API_KEY"
success "RUNNER_API_KEY"

gh secret set NOTIFY_CHAT_ID --repo "$CHILD_REPO" --body "$TELEGRAM_CHAT_ID"
success "NOTIFY_CHAT_ID"

# ── 3. Copy workflow templates ──
info "Copying workflow templates..."

TMPDIR=$(mktemp -d)
gh repo clone "$CHILD_REPO" "$TMPDIR/repo" -- --depth 1 2>/dev/null

mkdir -p "$TMPDIR/repo/.github/workflows"

for tmpl in implement.yml review.yml; do
  if [[ -f "$REPO_ROOT/templates/workflows/$tmpl" ]]; then
    cp "$REPO_ROOT/templates/workflows/$tmpl" "$TMPDIR/repo/.github/workflows/$tmpl"
    success "Copied $tmpl"
  fi
done

cd "$TMPDIR/repo"
if ! git diff --quiet 2>/dev/null; then
  git add .github/workflows/
  git commit -m "ci: add implement + review workflow templates"
  git push
  success "Workflows pushed to $CHILD_REPO"
else
  info "Workflows already up to date"
fi

rm -rf "$TMPDIR"

echo -e "\n${GREEN}${BOLD}Done!${RESET} Child repo $CHILD_REPO is ready."
echo -e "  Use ${BOLD}/build $CHILD_REPO${RESET} to trigger the implement pipeline."
echo ""
```

```bash
chmod +x scripts/setup-child-repo.sh
```

- [ ] **Step 2: Commit**

```bash
git add scripts/setup-child-repo.sh
git commit -m "feat: add setup-child-repo.sh for child repo secrets & workflows"
```
