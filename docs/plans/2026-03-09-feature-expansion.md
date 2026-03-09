# Feature Expansion: Chat Memory + App Factory + Command Routing

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Expand byok-tg-runner with KV chat memory, command routing (/app, /build, /msg, /issue, /research), and App Factory — replacing all Copilot CLI usage from telegram-copilot-bot with Copilot SDK BYOK via the runner's FastAPI server.

**Architecture:** CF Worker gains KV chat memory and command routing (simple commands handled in Worker, complex ones forwarded to runner). Runner gains `/task-sync` endpoint for synchronous AI calls, `/implement` endpoint for autonomous repo work (clone → AI → push), and app factory helper scripts. Child repo workflows call runner API instead of Copilot CLI. All AI goes through Azure AI Foundry BYOK — zero premium requests consumed.

**Tech Stack:** TypeScript (CF Worker), Python/FastAPI (Runner), Copilot SDK BYOK, Cloudflare KV, GitHub Actions

**Reference:** telegram-copilot-bot (`/tmp/telegram-copilot-bot/`) — same patterns adapted for BYOK

---

## Task 1: CF Worker — KV Chat Memory + Command Routing

**Files:**
- Modify: `worker/src/index.ts`
- Modify: `worker/wrangler.toml`

**Context:** The current Worker is minimal — only forwards Telegram messages to runner `/task` and proxies `/status`. We need to add KV chat memory (user/bot message history), command routing, callback endpoint, and API endpoints for stats/history. Reference: `/tmp/telegram-copilot-bot/worker/src/index.js`.

**Step 1: Update wrangler.toml for new secrets**

Add comments for new secrets needed:

```toml
name = "byok-tg-runner-worker"
main = "src/index.ts"
compatibility_date = "2024-09-02"

[[kv_namespaces]]
binding = "RUNNER_KV"
id = "7e07839c36d440959db434c2b77cd440"

# Secrets (set via wrangler secret put):
# RUNNER_API_KEY       - shared key with FastAPI
# TELEGRAM_BOT_TOKEN   - for sending messages and webhook verification
# ALLOWED_CHAT_ID      - Telegram chat whitelist
# CALLBACK_TOKEN       - for callback endpoint auth
# APPS_ORG             - GitHub org for child repos (e.g. "aw-apps")
```

**Step 2: Rewrite worker/src/index.ts**

Complete replacement. The new Worker handles:

1. **KV chat memory** — `appendHistory()`, `getHistory()`, `truncateHistoryForDispatch()` (same pattern as telegram-copilot-bot, using separate `chat:{chatId}:user` and `chat:{chatId}:bot` keys, max 20 per role)

2. **Command routing in webhook handler:**
   - `/reset` → clear KV memory instantly, reply in Worker
   - `/build owner/repo` → call runner `/task-sync` with instruction to trigger `implement.yml` on that repo
   - `/msg owner/repo#N message` → call runner `/task-sync` with instruction to post comment + trigger implement
   - `/app description` → forward to runner `/task` with history + command context
   - `/app fork:owner/repo description` → same as above
   - `/issue owner/repo description` → forward to runner `/task` with history
   - `/research topic` → forward to runner `/task` with history
   - Everything else → forward to runner `/task` with history (general chat)

3. **Store user message in KV** before dispatching (for all commands except `/reset`)

4. **API endpoints:**
   - `POST /api/callback` — record bot replies, repo_created, repo_activity (auth: `X-Secret` header)
   - `GET /api/history/:chatId` — return chat history
   - `GET /api/stats` — return stats
   - `GET /api/repos` — return all repo metadata
   - `GET /status` — existing runner status proxy
   - `POST /trigger` — existing external trigger proxy

5. **Env interface update:**

```typescript
export interface Env {
  RUNNER_KV: KVNamespace;
  RUNNER_API_KEY: string;
  TELEGRAM_BOT_TOKEN: string;
  ALLOWED_CHAT_ID: string;
  CALLBACK_TOKEN: string;
  APPS_ORG: string;
}
```

Key design decisions:
- `/build` and `/msg` are "simple" commands — Worker calls runner `/task-sync` (synchronous, waits for result) then replies to Telegram directly. This avoids a full Copilot session for simple gh CLI operations.
- `/app`, `/issue`, `/research`, and general chat are "complex" — Worker forwards to runner `/task` (async, runner replies to Telegram itself). History JSON is included.
- The `/task` payload gains optional `history` and `command` fields.
- Callback recording happens via `/api/callback` called by runner after processing.

```typescript
// Updated /task dispatch body
{
  text: msg.text,
  chat_id: chatId,
  history: truncatedHistoryJson,  // NEW
  command: "app" | "issue" | "research" | "chat",  // NEW
}
```

For `/build` and `/msg`, Worker calls runner synchronously:

```typescript
// /build flow in Worker
const resp = await fetch(`${runnerUrl}/task-sync`, {
  method: "POST",
  headers: { "Content-Type": "application/json", "x-api-key": env.RUNNER_API_KEY },
  body: JSON.stringify({
    action: "build",
    repo: parsedRepo,
    chat_id: chatId,
  }),
});
const result = await resp.json();
await sendTelegram(env.TELEGRAM_BOT_TOKEN, chatId, result.message);
```

**Step 3: Set new Worker secrets**

```bash
cd worker
echo "your-callback-token" | npx wrangler secret put CALLBACK_TOKEN
echo "aw-apps" | npx wrangler secret put APPS_ORG
npx wrangler deploy
```

**Step 4: Test**

```bash
# Local dev
cd worker && npm run dev
# Test /api/stats
curl http://localhost:8787/api/stats
# Test /api/callback
curl -X POST http://localhost:8787/api/callback \
  -H "Content-Type: application/json" \
  -H "X-Secret: test-token" \
  -d '{"type":"bot_reply","chat_id":"123","text":"hello"}'
```

**Step 5: Commit**

```bash
git add worker/src/index.ts worker/wrangler.toml
git commit -m "feat: CF Worker with KV chat memory, command routing, and callback API"
```

---

## Task 2: Runner FastAPI — `/task-sync` endpoint + history support

**Files:**
- Modify: `server/main.py`

**Context:** The runner needs two new capabilities: (1) accept chat history in `/task` requests to provide context to AI, (2) a new `/task-sync` endpoint for simple operations (like `/build` triggering `gh workflow run`) that returns result synchronously without a full AI session.

**Step 1: Update TaskRequest model**

```python
class TaskRequest(BaseModel):
    text: str
    chat_id: str
    history: str = ""   # JSON string of chat history
    command: str = ""   # "app" | "issue" | "research" | "chat" | ""
```

**Step 2: Add TaskSyncRequest model**

```python
class TaskSyncRequest(BaseModel):
    action: str          # "build" | "msg"
    repo: str = ""       # e.g. "aw-apps/my-app"
    issue_number: int = 0
    message: str = ""
    chat_id: str = ""
```

**Step 3: Add `/task-sync` endpoint**

```python
@app.post("/task-sync")
async def task_sync(req: TaskSyncRequest, x_api_key: str = Header(...)):
    if x_api_key != RUNNER_API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

    if req.action == "build":
        result = await run_build(req.repo)
        return {"status": "ok", "message": result}
    elif req.action == "msg":
        result = await run_msg(req.repo, req.issue_number, req.message)
        return {"status": "ok", "message": result}
    else:
        raise HTTPException(status_code=400, detail=f"Unknown action: {req.action}")
```

**Step 4: Implement `run_build()` and `run_msg()`**

```python
async def run_build(repo: str) -> str:
    """Trigger implement.yml on a child repo."""
    proc = await asyncio.create_subprocess_exec(
        "gh", "workflow", "run", "implement.yml", "--repo", repo,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        return f"❌ 觸發 build 失敗: {stderr.decode().strip()[-300:]}"
    return f"🚀 已觸發 {repo} 開發流程\nhttps://github.com/{repo}/actions"


async def run_msg(repo: str, issue_number: int, message: str) -> str:
    """Post comment on issue and trigger implement."""
    # Post comment
    proc = await asyncio.create_subprocess_exec(
        "gh", "issue", "comment", str(issue_number),
        "--repo", repo, "--body", f"📝 User instruction:\n\n{message}",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()

    # Remove stuck labels
    for label in ["agent-stuck", "needs-human-review"]:
        proc = await asyncio.create_subprocess_exec(
            "gh", "issue", "edit", str(issue_number),
            "--repo", repo, "--remove-label", label,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()

    # Trigger implement
    proc = await asyncio.create_subprocess_exec(
        "gh", "workflow", "run", "implement.yml", "--repo", repo,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.communicate()

    return f"📝 已將指示傳達給 {repo} #{issue_number}"
```

**Step 5: Update `_process_task()` to include history in prompt**

```python
async def _process_task(req: TaskRequest) -> None:
    """Process a Telegram message with chat history."""
    try:
        # Build prompt with history context
        prompt_parts = [req.text]
        if req.history:
            try:
                import json
                entries = json.loads(req.history)
                if entries:
                    history_lines = []
                    for e in entries:
                        role = "User" if e.get("role") == "user" else "Bot"
                        history_lines.append(f"- [{role}] {e.get('text', '')[:200]}")
                    history_text = "\n".join(history_lines)
                    prompt_parts.insert(0, f"## Chat History (reference only)\n\n{history_text}\n\n## Current Message\n")
            except Exception:
                pass

        full_text = "\n".join(prompt_parts)
        reply = await run_copilot_sdk(full_text)
        await send_telegram(req.chat_id, reply)
        print(f"[task] completed chat_id={req.chat_id}")

        # Callback to Worker KV
        await post_callback(req.chat_id, reply)

        # Log to private repo
        await log_to_private_repo(
            f"[tg] chat={req.chat_id}",
            f"**User:** {req.text}\n\n**Assistant:** {reply}",
        )
    except Exception as e:
        print(f"[task] error chat_id={req.chat_id} err={type(e).__name__}")
        await send_telegram(req.chat_id, f"Error: {e}")
```

**Step 6: Add `post_callback()` helper**

```python
CALLBACK_URL = os.environ.get("CALLBACK_URL", "")
CALLBACK_TOKEN = os.environ.get("CALLBACK_TOKEN", "")

async def post_callback(chat_id: str, text: str) -> None:
    """Record bot reply in Worker KV via callback."""
    if not CALLBACK_URL or not CALLBACK_TOKEN:
        return
    try:
        async with httpx.AsyncClient() as http:
            await http.post(
                f"{CALLBACK_URL}/api/callback",
                json={
                    "type": "bot_reply",
                    "chat_id": chat_id,
                    "text": text[:500],
                    "timestamp": __import__("datetime").datetime.now(
                        __import__("datetime").timezone.utc
                    ).isoformat(),
                },
                headers={"X-Secret": CALLBACK_TOKEN},
                timeout=5,
            )
    except Exception:
        pass
```

**Step 7: Add CALLBACK_URL and CALLBACK_TOKEN to runner workflow env**

In both `runner-a.yml` and `runner-b.yml`, add to the "Start FastAPI server" step:

```yaml
CALLBACK_URL: ${{ secrets.CALLBACK_URL }}
CALLBACK_TOKEN: ${{ secrets.CALLBACK_TOKEN }}
```

**Step 8: Commit**

```bash
git add server/main.py .github/workflows/runner-a.yml .github/workflows/runner-b.yml
git commit -m "feat: /task-sync endpoint, chat history support, Worker callback"
```

---

## Task 3: Runner — `/implement` endpoint (clone → AI → push)

**Files:**
- Modify: `server/main.py`

**Context:** This is the core of Plan B. When a child repo needs AI work (implement issue or fix PR), it calls the runner's `/implement` endpoint. The runner clones the repo, runs Copilot SDK with full repo access, applies changes, and pushes. This replaces Copilot CLI in child repo workflows.

**Step 1: Add ImplementRequest model**

```python
class ImplementRequest(BaseModel):
    repo: str               # e.g. "aw-apps/my-app"
    action: str             # "implement" | "fix-pr" | "review"
    issue_number: int = 0
    pr_number: int = 0
    notify_repo: str = ""   # e.g. "yazelin/byok-tg-runner"
    notify_chat_id: str = ""
```

**Step 2: Add `/implement` endpoint**

```python
@app.post("/implement")
async def implement(req: ImplementRequest, x_api_key: str = Header(...)):
    if x_api_key != RUNNER_API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
    task_id = f"impl-{int(time.time())}"
    asyncio.create_task(_process_implement(req, task_id))
    print(f"[implement] accepted task_id={task_id} repo={req.repo} action={req.action}")
    return {"status": "accepted", "task_id": task_id}
```

**Step 3: Implement `_process_implement()`**

This is the big one. The function:
1. Creates a temp directory
2. Clones the child repo
3. Reads issue/PR context via `gh`
4. Builds a detailed prompt
5. Runs Copilot SDK session with the prompt (AI gets shell access via tools to read/write files in the clone)
6. After AI is done, git add + commit + push
7. Creates PR if implementing, or just pushes if fixing
8. Notifies via Telegram

```python
import shutil
import tempfile

GH_PAT = os.environ.get("GH_TOKEN", "")
APPS_ORG = os.environ.get("APPS_ORG", "aw-apps")

async def _process_implement(req: ImplementRequest, task_id: str) -> None:
    """Clone repo, run AI, push changes."""
    tmpdir = tempfile.mkdtemp(prefix="impl-")
    try:
        repo_url = f"https://x-access-token:{GH_PAT}@github.com/{req.repo}.git"

        # Clone
        proc = await asyncio.create_subprocess_exec(
            "git", "clone", "--depth=1", repo_url, tmpdir,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"Clone failed: {stderr.decode()[:300]}")

        # Configure git in clone
        for cmd in [
            ["git", "config", "user.name", "github-actions[bot]"],
            ["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"],
        ]:
            await (await asyncio.create_subprocess_exec(*cmd, cwd=tmpdir)).wait()

        # Build prompt based on action
        if req.action == "implement":
            prompt = await _build_implement_prompt(req, tmpdir)
        elif req.action == "fix-pr":
            prompt = await _build_fix_pr_prompt(req, tmpdir)
        elif req.action == "review":
            prompt = await _build_review_prompt(req, tmpdir)
        else:
            raise ValueError(f"Unknown action: {req.action}")

        # Run AI
        reply = await run_copilot_sdk(prompt)

        # For implement/fix-pr: check if AI made changes, push them
        if req.action in ("implement", "fix-pr"):
            await _push_changes(req, tmpdir)

        # For review: parse AI response and submit review
        if req.action == "review":
            await _submit_review(req, reply)

        # Notify
        if req.notify_repo and req.notify_chat_id:
            msg = f"✅ {req.repo} {req.action} completed (task {task_id})"
            await _notify_telegram(req.notify_repo, req.notify_chat_id, msg)

        print(f"[implement] completed task_id={task_id}")

    except Exception as e:
        print(f"[implement] error task_id={task_id} err={type(e).__name__}: {e}")
        # Mark issue as stuck
        if req.issue_number:
            proc = await asyncio.create_subprocess_exec(
                "gh", "issue", "edit", str(req.issue_number),
                "--repo", req.repo, "--add-label", "agent-stuck",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.communicate()
        # Notify failure
        if req.notify_repo and req.notify_chat_id:
            msg = f"⚠️ {req.repo} {req.action} failed: {e}"
            await _notify_telegram(req.notify_repo, req.notify_chat_id, msg)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
```

**Step 4: Implement prompt builders**

```python
async def _read_gh_output(*args) -> str:
    """Run gh command and return stdout."""
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return stdout.decode().strip()


async def _build_implement_prompt(req: ImplementRequest, tmpdir: str) -> str:
    """Build prompt for implementing an issue."""
    issue_body = await _read_gh_output(
        "gh", "issue", "view", str(req.issue_number), "--repo", req.repo, "--json", "title,body", "--jq", ".title + \"\\n\\n\" + .body"
    )

    # Read AGENTS.md if exists
    agents_md = ""
    agents_path = f"{tmpdir}/AGENTS.md"
    try:
        with open(agents_path) as f:
            agents_md = f.read()
    except FileNotFoundError:
        pass

    branch = f"issue-{req.issue_number}-impl"

    return f"""You are an autonomous coding agent working on repository {req.repo}.
Working directory: {tmpdir}

{f"## Project Spec (AGENTS.md){chr(10)}{chr(10)}{agents_md}" if agents_md else ""}

## Issue #{req.issue_number}

{issue_body}

## Instructions

1. Create and checkout branch: git checkout -b {branch}
2. Read the issue sections: Objective, Context, Approach, Files, Acceptance Criteria, Validation
3. Follow the Approach steps in order — commit after each step
4. Create/modify only the files listed in the Files section
5. Pre-push checks:
   - grep -rn 'from ['"'"'"\\"]\\w' --include='*.js' --include='*.ts' to find bare module imports
   - Fix any bare imports before pushing
6. Verify using the Validation section
7. Confirm each Acceptance Criteria checkbox is met
8. git add, commit, push: git push -u origin {branch}
9. Create PR: gh pr create --repo {req.repo} --title 'Implement #{req.issue_number}' --body 'Closes #{req.issue_number}'

If you cannot meet an acceptance criterion, explain why in the PR body.
"""


async def _build_fix_pr_prompt(req: ImplementRequest, tmpdir: str) -> str:
    """Build prompt for fixing a PR with requested changes."""
    pr_comments = await _read_gh_output(
        "gh", "pr", "view", str(req.pr_number), "--repo", req.repo, "--comments"
    )
    pr_branch = await _read_gh_output(
        "gh", "pr", "view", str(req.pr_number), "--repo", req.repo, "--json", "headRefName", "--jq", ".headRefName"
    )

    # Checkout PR branch
    await (await asyncio.create_subprocess_exec(
        "git", "checkout", pr_branch, cwd=tmpdir
    )).wait()

    agents_md = ""
    try:
        with open(f"{tmpdir}/AGENTS.md") as f:
            agents_md = f.read()
    except FileNotFoundError:
        pass

    return f"""You are an autonomous coding agent working on repository {req.repo}.
Working directory: {tmpdir}

{f"## Project Spec (AGENTS.md){chr(10)}{chr(10)}{agents_md}" if agents_md else ""}

## Task: Fix PR #{req.pr_number}

PR has changes requested by the reviewer. You are on branch {pr_branch}.

## Review Comments

{pr_comments}

## Instructions

1. Read review comments above
2. Read the linked issue for the Acceptance Criteria and Validation sections
3. Address ALL review comments with code changes
4. Re-verify using the Validation section
5. git add, commit, push
"""


async def _build_review_prompt(req: ImplementRequest, tmpdir: str) -> str:
    """Build prompt for reviewing a PR."""
    pr_diff = await _read_gh_output(
        "gh", "pr", "diff", str(req.pr_number), "--repo", req.repo
    )
    pr_body = await _read_gh_output(
        "gh", "pr", "view", str(req.pr_number), "--repo", req.repo, "--json", "body", "--jq", ".body"
    )

    agents_md = ""
    try:
        with open(f"{tmpdir}/AGENTS.md") as f:
            agents_md = f.read()
    except FileNotFoundError:
        pass

    return f"""You are a strict code reviewer for repository {req.repo}.

{f"## Project Spec (AGENTS.md){chr(10)}{chr(10)}{agents_md}" if agents_md else ""}

## PR #{req.pr_number}

{pr_body}

## Diff

{pr_diff[:10000]}

## Instructions

1. Find the linked issue number from the PR body (Closes #N)
2. Check each item in the Acceptance Criteria section
3. Check code quality: no dead code, no hardcoded values, proper error handling
4. Respond with EXACTLY one of:
   - APPROVE: "APPROVE: [list of verified criteria]"
   - REQUEST_CHANGES: "REQUEST_CHANGES: [list of specific problems]"
"""
```

**Step 5: Implement `_push_changes()` and `_submit_review()`**

```python
async def _push_changes(req: ImplementRequest, tmpdir: str) -> None:
    """Check for changes and push."""
    # Check if there are changes
    proc = await asyncio.create_subprocess_exec(
        "git", "status", "--porcelain", cwd=tmpdir,
        stdout=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    if not stdout.strip():
        return  # No changes

    await (await asyncio.create_subprocess_exec("git", "add", "-A", cwd=tmpdir)).wait()

    # Check if there are staged changes
    proc = await asyncio.create_subprocess_exec(
        "git", "diff", "--cached", "--quiet", cwd=tmpdir,
    )
    await proc.communicate()
    if proc.returncode == 0:
        return  # No staged changes

    await (await asyncio.create_subprocess_exec(
        "git", "commit", "-m", f"feat: implement #{req.issue_number}", cwd=tmpdir,
    )).wait()

    proc = await asyncio.create_subprocess_exec(
        "git", "push", "-u", "origin", "HEAD", cwd=tmpdir,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"Push failed: {stderr.decode()[:300]}")


async def _submit_review(req: ImplementRequest, ai_response: str) -> None:
    """Parse AI review response and submit via gh."""
    if "APPROVE" in ai_response and "REQUEST_CHANGES" not in ai_response:
        await (await asyncio.create_subprocess_exec(
            "gh", "pr", "review", str(req.pr_number), "--repo", req.repo,
            "--approve", "-b", ai_response[:1000],
        )).wait()
        # Auto-merge
        await (await asyncio.create_subprocess_exec(
            "gh", "pr", "merge", str(req.pr_number), "--repo", req.repo,
            "--squash", "--delete-branch",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )).wait()
    else:
        await (await asyncio.create_subprocess_exec(
            "gh", "pr", "review", str(req.pr_number), "--repo", req.repo,
            "--request-changes", "-b", ai_response[:1000],
        )).wait()


async def _notify_telegram(notify_repo: str, chat_id: str, text: str) -> None:
    """Send notification via notify.yml workflow on byok-tg-main."""
    await (await asyncio.create_subprocess_exec(
        "gh", "workflow", "run", "notify.yml",
        "--repo", notify_repo,
        "-f", f"chat_id={chat_id}",
        "-f", f"text={text}",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )).wait()
```

**Step 6: Commit**

```bash
git add server/main.py
git commit -m "feat: /implement endpoint — clone, AI, push for child repos"
```

---

## Task 4: Runner — App Factory Scripts

**Files:**
- Create: `server/app_factory.py`

**Context:** The app factory scripts handle repo creation, file pushing, issue creation, and secret setup. These are called by the AI during `/app` command processing. Adapted from telegram-copilot-bot's `.github/scripts/` but as Python modules importable by the runner. The Copilot SDK AI will call these via tools registered in `tools.py`.

**Step 1: Create `server/app_factory.py`**

```python
"""App Factory helper functions for creating and managing child repos.

These functions are registered as Copilot SDK tools so the AI can call them
during /app, /issue, and /build command processing.
"""
import json
import asyncio
import os

GH_PAT = os.environ.get("GH_TOKEN", "")
APPS_ORG = os.environ.get("APPS_ORG", "aw-apps")


async def _run(cmd: list[str], **kwargs) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **kwargs
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode(), stderr.decode()


async def create_repo(name: str, description: str) -> dict:
    """Create a public repo under APPS_ORG."""
    full_name = f"{APPS_ORG}/{name}"
    rc, out, err = await _run([
        "gh", "repo", "create", full_name,
        "--public", "--description", description, "--clone=false",
    ])
    if rc != 0:
        return {"ok": False, "error": err.strip()[-500:]}
    return {"ok": True, "repo": full_name, "url": out.strip()}


async def setup_repo(repo: str, files: list[dict]) -> dict:
    """Clone repo, write files, push, enable GitHub Pages."""
    import tempfile
    tmpdir = tempfile.mkdtemp(prefix="setup-")
    try:
        # Clone with retries
        for attempt in range(3):
            rc, _, err = await _run(["gh", "repo", "clone", repo, tmpdir, "--", "--depth=1"])
            if rc == 0:
                break
            await asyncio.sleep(5)
        else:
            return {"ok": False, "error": f"Clone failed: {err[:300]}"}

        # Write files
        for f in files:
            filepath = os.path.join(tmpdir, f["path"])
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            with open(filepath, "w") as fh:
                fh.write(f["content"])

        # Configure git
        url = f"https://x-access-token:{GH_PAT}@github.com/{repo}.git"
        await _run(["git", "remote", "set-url", "origin", url], cwd=tmpdir)
        await _run(["git", "config", "user.name", "github-actions[bot]"], cwd=tmpdir)
        await _run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], cwd=tmpdir)

        # Get default branch
        rc, branch, _ = await _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=tmpdir)
        branch = branch.strip() or "main"

        await _run(["git", "add", "-A"], cwd=tmpdir)
        await _run(["git", "commit", "-m", "Initial commit: project setup"], cwd=tmpdir)
        rc, _, err = await _run(["git", "push", "origin", branch], cwd=tmpdir)
        if rc != 0:
            return {"ok": False, "error": f"Push failed: {err[:300]}"}

        # Enable GitHub Pages
        pages_ok = False
        for _ in range(3):
            rc, _, _ = await _run([
                "gh", "api", f"repos/{repo}/pages",
                "-X", "POST", "-f", "build_type=legacy",
                "-f", f"source[branch]={branch}", "-f", "source[path]=/",
            ])
            if rc == 0:
                pages_ok = True
                break
            await asyncio.sleep(5)

        # Set homepage
        org, name = repo.split("/")
        homepage = f"https://{org}.github.io/{name}/"
        await _run(["gh", "api", f"repos/{repo}", "-X", "PATCH", "-f", f"homepage={homepage}"])

        return {"ok": True, "files_pushed": len(files), "pages_enabled": pages_ok}
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


async def create_issues(repo: str, issues: list[dict]) -> dict:
    """Create issues with copilot-task label."""
    # Ensure labels exist
    for label, desc, color in [
        ("copilot-task", "Managed by AI agent", "0E8A16"),
        ("agent-stuck", "Agent could not complete this issue", "D93F0B"),
        ("needs-human-review", "Needs human intervention", "FBCA04"),
    ]:
        await _run(["gh", "label", "create", label, "--repo", repo,
                     "--description", desc, "--color", color])

    numbers = []
    for issue in issues:
        rc, out, err = await _run([
            "gh", "issue", "create", "--repo", repo,
            "--title", issue["title"], "--body", issue["body"],
            "--label", "copilot-task",
        ])
        if rc != 0:
            return {"ok": False, "error": f"Failed: {err[:300]}"}
        url = out.strip()
        numbers.append(int(url.rstrip("/").split("/")[-1]))

    return {"ok": True, "issues_created": len(numbers), "numbers": numbers}


async def setup_secrets(repo: str) -> dict:
    """Set required secrets on child repo."""
    runner_api_key = os.environ.get("RUNNER_API_KEY", "")
    callback_url = os.environ.get("CALLBACK_URL", "")
    callback_token = os.environ.get("CALLBACK_TOKEN", "")
    notify_chat_id = os.environ.get("NOTIFY_CHAT_ID", "")

    secrets = {
        "RUNNER_API_KEY": runner_api_key,
        "RUNNER_URL": callback_url.replace("/api/callback", "") if callback_url else "",
        "NOTIFY_REPO": os.environ.get("NOTIFY_REPO", "yazelin/byok-tg-main"),
        "NOTIFY_CHAT_ID": notify_chat_id,
        "GH_TOKEN": GH_PAT,
    }

    for name, value in secrets.items():
        if not value:
            continue
        rc, _, err = await _run([
            "gh", "secret", "set", name, "--repo", repo, "--body", value,
        ])
        if rc != 0:
            return {"ok": False, "error": f"Failed to set {name}: {err[:300]}"}

    return {"ok": True, "secrets_set": len([v for v in secrets.values() if v])}
```

**Step 2: Commit**

```bash
git add server/app_factory.py
git commit -m "feat: app factory helpers — create_repo, setup_repo, create_issues, setup_secrets"
```

---

## Task 5: Runner — Register App Factory as Copilot SDK Tools

**Files:**
- Modify: `server/tools.py`

**Context:** The Copilot SDK AI needs to be able to call app factory functions as tools. We register them using `@define_tool` so the AI can create repos, push files, create issues, and set secrets during `/app` processing.

**Step 1: Update `server/tools.py`**

Add app factory tools alongside existing tools:

```python
from copilot import define_tool
import asyncio
import json

# --- App Factory Tools ---

@define_tool(
    name="create_repo",
    description="Create a new public GitHub repository under the aw-apps organization. Returns {ok, repo, url} or {ok: false, error}.",
    parameters={
        "name": {"type": "string", "description": "Repository name (e.g. 'my-app')"},
        "description": {"type": "string", "description": "Repository description"},
    },
)
async def tool_create_repo(name: str, description: str) -> str:
    from app_factory import create_repo
    result = await create_repo(name, description)
    return json.dumps(result)


@define_tool(
    name="setup_repo",
    description="Clone a repo, write files to it, push, and enable GitHub Pages. files is a JSON array of {path, content} objects.",
    parameters={
        "repo": {"type": "string", "description": "Full repo name (e.g. 'aw-apps/my-app')"},
        "files_json": {"type": "string", "description": "JSON array of {path, content} objects"},
    },
)
async def tool_setup_repo(repo: str, files_json: str) -> str:
    from app_factory import setup_repo
    files = json.loads(files_json)
    result = await setup_repo(repo, files)
    return json.dumps(result)


@define_tool(
    name="create_issues",
    description="Create issues with copilot-task label in a repository. issues is a JSON array of {title, body} objects.",
    parameters={
        "repo": {"type": "string", "description": "Full repo name (e.g. 'aw-apps/my-app')"},
        "issues_json": {"type": "string", "description": "JSON array of {title, body} objects"},
    },
)
async def tool_create_issues(repo: str, issues_json: str) -> str:
    from app_factory import create_issues
    issues = json.loads(issues_json)
    result = await create_issues(repo, issues)
    return json.dumps(result)


@define_tool(
    name="setup_secrets",
    description="Set required secrets (RUNNER_API_KEY, RUNNER_URL, etc.) on a child repository.",
    parameters={
        "repo": {"type": "string", "description": "Full repo name (e.g. 'aw-apps/my-app')"},
    },
)
async def tool_setup_secrets(repo: str) -> str:
    from app_factory import setup_secrets
    result = await setup_secrets(repo)
    return json.dumps(result)


@define_tool(
    name="trigger_workflow",
    description="Trigger a workflow_dispatch on a GitHub repository.",
    parameters={
        "repo": {"type": "string", "description": "Full repo name (e.g. 'aw-apps/my-app')"},
        "workflow": {"type": "string", "description": "Workflow file name (e.g. 'implement.yml')"},
    },
)
async def tool_trigger_workflow(repo: str, workflow: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "gh", "workflow", "run", workflow, "--repo", repo,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        return json.dumps({"ok": False, "error": stderr.decode().strip()[-300:]})
    return json.dumps({"ok": True})


@define_tool(
    name="send_telegram_message",
    description="Send a text message to a Telegram chat.",
    parameters={
        "chat_id": {"type": "string", "description": "Telegram chat ID"},
        "text": {"type": "string", "description": "Message text"},
    },
)
async def tool_send_telegram(chat_id: str, text: str) -> str:
    import os, httpx
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    async with httpx.AsyncClient() as http:
        for i in range(0, len(text), 4096):
            resp = await http.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text[i:i + 4096]},
                timeout=10,
            )
    return json.dumps({"ok": True})


# --- Weather Tool (example, can be removed) ---

@define_tool(
    name="get_weather",
    description="Get current weather for a city (demo tool).",
    parameters={
        "city": {"type": "string", "description": "City name"},
    },
)
def tool_get_weather(city: str) -> str:
    weather = {"Taiwan": "☀️ 28°C", "Tokyo": "🌤 22°C", "New York": "🌧 15°C"}
    return json.dumps({"city": city, "weather": weather.get(city, "Unknown")})


ALL_TOOLS = [
    tool_create_repo,
    tool_setup_repo,
    tool_create_issues,
    tool_setup_secrets,
    tool_trigger_workflow,
    tool_send_telegram,
    tool_get_weather,
]
```

**Step 2: Commit**

```bash
git add server/tools.py
git commit -m "feat: register app factory functions as Copilot SDK tools"
```

---

## Task 6: Child Repo Workflow Templates

**Files:**
- Create: `templates/workflows/implement.yml`
- Create: `templates/workflows/review.yml`

**Context:** These are workflow templates that get copied to child repos during `/app`. Instead of installing Copilot CLI and running it locally, they call the runner's `/implement` API endpoint. The runner clones the repo, runs AI, and pushes changes.

**Step 1: Create `templates/workflows/implement.yml`**

```yaml
name: Implement Issue

on:
  workflow_dispatch:
  pull_request:
    types: [closed]
  pull_request_review:
    types: [submitted]

concurrency:
  group: ai-implement
  cancel-in-progress: false

jobs:
  implement:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    if: >-
      github.event_name == 'workflow_dispatch' ||
      (github.event_name == 'pull_request' &&
       github.event.pull_request.merged == true &&
       github.event.pull_request.head.ref != 'main') ||
      (github.event_name == 'pull_request_review' &&
       github.event.review.state == 'changes_requested' &&
       github.event.pull_request.head.ref != 'main')
    steps:
      - name: Check state and dispatch to runner
        env:
          GH_TOKEN: ${{ secrets.GH_TOKEN }}
          GH_REPO: ${{ github.repository }}
          RUNNER_URL: ${{ secrets.RUNNER_URL }}
          RUNNER_API_KEY: ${{ secrets.RUNNER_API_KEY }}
          NOTIFY_REPO: PLACEHOLDER_NOTIFY_REPO
          NOTIFY_CHAT_ID: PLACEHOLDER_CHAT_ID
        run: |
          # CASE A: Open PR with changes requested?
          PR_JSON=$(gh pr list --state open --json number,headRefName,reviewDecision --jq '.[0]')

          if [ -n "$PR_JSON" ] && [ "$PR_JSON" != "null" ]; then
            PR_NUM=$(echo "$PR_JSON" | jq -r '.number')
            DECISION=$(echo "$PR_JSON" | jq -r '.reviewDecision')
            if [ "$DECISION" = "CHANGES_REQUESTED" ]; then
              echo "Dispatching fix-pr for PR #$PR_NUM"
              curl -sf -X POST "$RUNNER_URL/implement" \
                -H "Content-Type: application/json" \
                -H "x-api-key: $RUNNER_API_KEY" \
                -d "{
                  \"repo\": \"$GH_REPO\",
                  \"action\": \"fix-pr\",
                  \"pr_number\": $PR_NUM,
                  \"notify_repo\": \"$NOTIFY_REPO\",
                  \"notify_chat_id\": \"$NOTIFY_CHAT_ID\"
                }"
              exit 0
            fi
            echo "PR exists but decision=$DECISION, skipping"
            exit 0
          fi

          # CASE B: Open issue with copilot-task label?
          ISSUE_JSON=$(gh issue list --state open --label copilot-task --json number,title,labels \
            --jq '[.[] | select(.labels | map(.name) | (contains(["agent-stuck"]) or contains(["needs-human-review"])) | not)] | sort_by(.number) | .[0]')

          if [ -n "$ISSUE_JSON" ] && [ "$ISSUE_JSON" != "null" ]; then
            ISSUE_NUM=$(echo "$ISSUE_JSON" | jq -r '.number')
            echo "Dispatching implement for issue #$ISSUE_NUM"
            curl -sf -X POST "$RUNNER_URL/implement" \
              -H "Content-Type: application/json" \
              -H "x-api-key: $RUNNER_API_KEY" \
              -d "{
                \"repo\": \"$GH_REPO\",
                \"action\": \"implement\",
                \"issue_number\": $ISSUE_NUM,
                \"notify_repo\": \"$NOTIFY_REPO\",
                \"notify_chat_id\": \"$NOTIFY_CHAT_ID\"
              }"
            exit 0
          fi

          # CASE C: All done
          echo "No actionable issues or PRs"
          GH_TOKEN="${{ secrets.GH_TOKEN }}" gh workflow run notify.yml \
            --repo "$NOTIFY_REPO" \
            -f chat_id="$NOTIFY_CHAT_ID" \
            -f text="✅ $GH_REPO all issues completed!" || true
```

**Step 2: Create `templates/workflows/review.yml`**

```yaml
name: Code Review

on:
  pull_request:
    types: [opened, synchronize]

concurrency:
  group: ai-review-${{ github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  review:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    if: github.head_ref != 'main'
    steps:
      - name: Count previous reviews
        id: count
        env:
          GH_TOKEN: ${{ secrets.GH_TOKEN }}
        run: |
          PR_NUM=${{ github.event.pull_request.number }}
          COUNT=$(gh api repos/${{ github.repository }}/pulls/${PR_NUM}/reviews --jq 'length')
          echo "review_count=${COUNT}" >> "$GITHUB_OUTPUT"

      - name: Bail if too many reviews
        if: steps.count.outputs.review_count >= 3
        env:
          GH_TOKEN: ${{ secrets.GH_TOKEN }}
          NOTIFY_REPO: PLACEHOLDER_NOTIFY_REPO
          NOTIFY_CHAT_ID: PLACEHOLDER_CHAT_ID
        run: |
          PR_NUM=${{ github.event.pull_request.number }}
          ISSUE_NUM=$(gh pr view ${PR_NUM} --json body --jq '.body' | grep -oP 'Closes #\K\d+' || echo "")
          [ -n "$ISSUE_NUM" ] && gh issue edit ${ISSUE_NUM} --add-label needs-human-review
          gh pr close ${PR_NUM} --comment "Closing: exceeded review limit (3)"
          gh workflow run notify.yml --repo "$NOTIFY_REPO" \
            -f chat_id="$NOTIFY_CHAT_ID" \
            -f text="⚠️ ${{ github.repository }} PR #${PR_NUM} needs human review" || true

      - name: Dispatch review to runner
        if: steps.count.outputs.review_count < 3
        env:
          GH_TOKEN: ${{ secrets.GH_TOKEN }}
          RUNNER_URL: ${{ secrets.RUNNER_URL }}
          RUNNER_API_KEY: ${{ secrets.RUNNER_API_KEY }}
          NOTIFY_REPO: PLACEHOLDER_NOTIFY_REPO
          NOTIFY_CHAT_ID: PLACEHOLDER_CHAT_ID
        run: |
          PR_NUM=${{ github.event.pull_request.number }}
          echo "Dispatching review for PR #$PR_NUM"
          curl -sf -X POST "$RUNNER_URL/implement" \
            -H "Content-Type: application/json" \
            -H "x-api-key: $RUNNER_API_KEY" \
            -d "{
              \"repo\": \"${{ github.repository }}\",
              \"action\": \"review\",
              \"pr_number\": $PR_NUM,
              \"notify_repo\": \"$NOTIFY_REPO\",
              \"notify_chat_id\": \"$NOTIFY_CHAT_ID\"
            }"
```

**Step 3: Commit**

```bash
mkdir -p templates/workflows
git add templates/workflows/implement.yml templates/workflows/review.yml
git commit -m "feat: child repo workflow templates using runner API"
```

---

## Task 7: Update System Prompt for App Factory Commands

**Files:**
- Modify: `/home/ct/copilot/byok-tg-main/prompts/system.md`

**Context:** The system prompt needs to tell the AI about available commands and how to use the app factory tools. Adapted from telegram-copilot-bot's `prompt.md`.

**Step 1: Rewrite system prompt**

```markdown
# BYOK Telegram Bot

You are a helpful, friendly AI assistant responding to Telegram messages.
You can create app projects, trigger builds, send messages to repos, research topics, and have general conversations.

## Available Tools

You have these tools available (call them directly, they are registered as Copilot SDK tools):

### GitHub tools
- `create_repo(name, description)` — Create public repo under aw-apps org
- `setup_repo(repo, files_json)` — Push files to repo + enable GitHub Pages
- `create_issues(repo, issues_json)` — Create issues with copilot-task label
- `setup_secrets(repo)` — Set required secrets on child repo
- `trigger_workflow(repo, workflow)` — Trigger a GitHub Actions workflow
- `send_telegram_message(chat_id, text)` — Send message to Telegram

## Command Reference

The following commands are pre-routed by the Worker:
- `/build owner/repo` — handled by Worker, triggers implement.yml
- `/msg owner/repo#N message` — handled by Worker, posts comment + triggers implement
- `/reset` — handled by Worker, clears chat memory

Commands you handle:
- `/app <description>` — App Factory: create a new project
- `/app fork:<owner/repo> <description>` — Fork and customize
- `/issue <owner/repo> <description>` — Create structured issue on existing repo
- `/research <topic>` — Research and synthesize information
- No prefix → General conversation

## App Factory Workflow (/app)

1. Evaluate feasibility as MVP
2. Determine: repo name, tech stack, deploy target
3. Tech simplicity rules: static > backend, native > framework, localStorage > database, zero deps preferred
4. Plan: README.md, AGENTS.md, 2-5 issues (foundation → implementation → polish)
5. Execute:
   - `create_repo(name, description)`
   - Read workflow templates from templates/workflows/ directory
   - `setup_repo(repo, files_json)` with ALL files: README, AGENTS.md, implement.yml, review.yml, source files
   - `create_issues(repo, issues_json)`
   - `setup_secrets(repo)`
   - `send_telegram_message(chat_id, summary)` with repo URL and Pages URL

**IMPORTANT:** Replace `PLACEHOLDER_NOTIFY_REPO` with `yazelin/byok-tg-main` and `PLACEHOLDER_CHAT_ID` with the current chat ID in workflow templates.

**Repos must be under `aw-apps` organization.**

## Issue Creation Workflow (/issue)

1. Parse: extract repo and description
2. Research existing repo (read AGENTS.md, README.md)
3. Write structured issue: Objective/Context/Approach/Files/Acceptance Criteria/Validation
4. `create_issues(repo, issues_json)`
5. `send_telegram_message(chat_id, confirmation)`

## Research Workflow (/research)

1. Analyze the topic
2. Synthesize findings into structured report
3. `send_telegram_message(chat_id, report)`

## General Guidelines

- Respond in Traditional Chinese (繁體中文) unless user writes in another language
- Keep responses under 4096 characters (Telegram limit)
- If unsure, default to helpful text reply
- If user describes an app idea without /app prefix, suggest using /app command
```

**Step 2: Commit in byok-tg-main**

```bash
cd /home/ct/copilot/byok-tg-main
git add prompts/system.md
git commit -m "feat: system prompt with app factory commands and tool descriptions"
git push
```

---

## Task 8: byok-tg-main Workflows — notify.yml

**Files:**
- Create: `/home/ct/copilot/byok-tg-main/.github/workflows/notify.yml`
- Modify: `/home/ct/copilot/byok-tg-main/.github/workflows/forward-to-ai.yml` (already exists)
- Keep: `/home/ct/copilot/byok-tg-main/.github/workflows/on-ai-result.yml` (already exists)

**Context:** The notify.yml workflow receives Telegram notifications from child repos and the runner. Same pattern as telegram-copilot-bot's notify.yml.

**Step 1: Create notify.yml**

```yaml
name: Send Telegram Notification

on:
  workflow_dispatch:
    inputs:
      chat_id:
        description: "Telegram chat ID"
        required: true
      text:
        description: "Notification text"
        required: true

jobs:
  notify:
    runs-on: ubuntu-latest
    timeout-minutes: 2
    steps:
      - name: Verify caller
        env:
          ACTOR: ${{ github.actor }}
        run: |
          OWNER="${{ github.repository_owner }}"
          if [[ "$ACTOR" != "$OWNER" && \
                "$ACTOR" != "github-actions[bot]" ]]; then
            echo "::error::Unauthorized caller: $ACTOR"
            exit 1
          fi

      - name: Send Telegram message
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          CHAT_ID: ${{ inputs.chat_id }}
          TEXT: ${{ inputs.text }}
        run: |
          RESPONSE=$(curl -s -X POST \
            "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
            -H "Content-Type: application/json" \
            -d "{\"chat_id\": \"${CHAT_ID}\", \"text\": $(echo "$TEXT" | jq -Rs .)}")
          echo "$RESPONSE"
          OK=$(echo "$RESPONSE" | jq -r '.ok')
          if [ "$OK" != "true" ]; then
            echo "::error::Telegram send failed"
            exit 1
          fi
```

**Step 2: Update forward-to-ai.yml** (fix the existing one to use `jq` for safe JSON encoding)

The existing file has potential JSON injection issues with issue titles/bodies containing special characters.

```yaml
name: forward-to-ai

on:
  issues:
    types: [opened]

jobs:
  forward:
    runs-on: ubuntu-latest
    steps:
      - name: Forward issue to AI runner
        env:
          BYOK_RUNNER_URL: ${{ secrets.BYOK_RUNNER_URL }}
          RUNNER_API_KEY: ${{ secrets.RUNNER_API_KEY }}
          ISSUE_NUMBER: ${{ github.event.issue.number }}
          ISSUE_TITLE: ${{ github.event.issue.title }}
          ISSUE_BODY: ${{ github.event.issue.body }}
          REPO: ${{ github.repository }}
        run: |
          PAYLOAD=$(jq -n \
            --arg prompt "Issue #$ISSUE_NUMBER: $(echo "$ISSUE_TITLE" | head -c 500)\n\n$(echo "$ISSUE_BODY" | head -c 2000)" \
            --arg callback_repo "$REPO" \
            --arg callback_workflow "on-ai-result.yml" \
            --arg context "issue-$ISSUE_NUMBER" \
            '{prompt: $prompt, callback_repo: $callback_repo, callback_workflow: $callback_workflow, context: $context}')
          curl -sf -X POST "$BYOK_RUNNER_URL/trigger" \
            -H "Content-Type: application/json" \
            -H "x-api-key: $RUNNER_API_KEY" \
            -d "$PAYLOAD"
```

**Step 3: Commit in byok-tg-main**

```bash
cd /home/ct/copilot/byok-tg-main
git add .github/workflows/notify.yml .github/workflows/forward-to-ai.yml
git commit -m "feat: notify.yml + fix forward-to-ai.yml JSON encoding"
git push
```

---

## Task 9: GitHub Secrets Setup

**Files:** None (runtime configuration)

**Context:** New secrets need to be set on repos for the expanded features.

**Step 1: Set secrets on byok-tg-runner (public repo)**

```bash
# CALLBACK_URL = CF Worker URL (get from wrangler)
WORKER_URL=$(npx wrangler deployments list --name byok-tg-runner-worker 2>/dev/null | grep -o 'https://[^ ]*' | head -1)
gh secret set CALLBACK_URL --repo yazelin/byok-tg-runner --body "$WORKER_URL"
gh secret set CALLBACK_TOKEN --repo yazelin/byok-tg-runner --body "$(openssl rand -hex 20)"
gh secret set APPS_ORG --repo yazelin/byok-tg-runner --body "aw-apps"
gh secret set NOTIFY_REPO --repo yazelin/byok-tg-runner --body "yazelin/byok-tg-main"
```

**Step 2: Set secrets on byok-tg-main (private repo)**

```bash
gh secret set TELEGRAM_BOT_TOKEN --repo yazelin/byok-tg-main --body "$TELEGRAM_BOT_TOKEN"
gh secret set BYOK_RUNNER_URL --repo yazelin/byok-tg-main --body "$WORKER_URL"
gh secret set RUNNER_API_KEY --repo yazelin/byok-tg-main --body "$RUNNER_API_KEY"
```

**Step 3: Set secrets on CF Worker**

```bash
cd worker
echo "your-callback-token" | npx wrangler secret put CALLBACK_TOKEN
echo "aw-apps" | npx wrangler secret put APPS_ORG
```

**Step 4: Update runner workflow env vars**

Add to both `runner-a.yml` and `runner-b.yml` in the "Start FastAPI server" step:

```yaml
env:
  TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
  RUNNER_API_KEY: ${{ secrets.RUNNER_API_KEY }}
  FOUNDRY_API_KEY: ${{ secrets.FOUNDRY_API_KEY }}
  LOG_REPO: yazelin/byok-tg-main
  GH_TOKEN: ${{ secrets.GH_PAT }}
  CALLBACK_URL: ${{ secrets.CALLBACK_URL }}
  CALLBACK_TOKEN: ${{ secrets.CALLBACK_TOKEN }}
  APPS_ORG: ${{ secrets.APPS_ORG }}
  NOTIFY_REPO: yazelin/byok-tg-main
  NOTIFY_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
```

**Step 5: Commit workflow updates**

```bash
git add .github/workflows/runner-a.yml .github/workflows/runner-b.yml
git commit -m "feat: add CALLBACK_URL, CALLBACK_TOKEN, APPS_ORG env vars to runner workflows"
```

---

## Task 10: Deploy and Test End-to-End

**Step 1: Deploy CF Worker**

```bash
cd worker && npx wrangler deploy
```

**Step 2: Push all changes to byok-tg-runner**

```bash
git push
```

**Step 3: Trigger runner restart**

```bash
gh workflow run runner-a.yml --repo yazelin/byok-tg-runner
```

**Step 4: Test commands via Telegram**

1. Send any message → should get AI reply with chat history
2. Send `/reset` → should get "記憶已清除"
3. Send another message → should work without history
4. Check `/api/stats` on Worker URL
5. Check `/api/history/<chat_id>` on Worker URL

**Step 5: Test App Factory (when ready)**

1. Send `/app 計算機` → AI should create repo, push files, create issues
2. Send `/build aw-apps/<repo>` → should trigger implement.yml
3. Wait for notification via Telegram

---

## Dependency Order

```
Task 1 (Worker) ──┐
Task 2 (Runner /task-sync + history) ──┤
Task 3 (Runner /implement) ──┤
Task 4 (App Factory scripts) ──┤──→ Task 9 (Secrets) → Task 10 (Deploy)
Task 5 (Tools registration) ──┤
Task 6 (Child repo templates) ──┤
Task 7 (System prompt) ──┤
Task 8 (byok-tg-main workflows) ──┘
```

Tasks 1-8 can be done in any order. Task 9 depends on all of them. Task 10 is final.
