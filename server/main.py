import asyncio
import json
import logging
import os
import shutil
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

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
CALLBACK_URL = os.environ.get("CALLBACK_URL", "")
CALLBACK_TOKEN = os.environ.get("CALLBACK_TOKEN", "")
GH_PAT = os.environ.get("GH_TOKEN", "")
APPS_ORG = os.environ.get("APPS_ORG", "aw-apps")

START_TIME = time.time()
client: CopilotClient = None


def load_tools():
    """Load tools from server/tools.py."""
    try:
        from tools import ALL_TOOLS
        print(f"[init] loaded {len(ALL_TOOLS)} tools")
        return ALL_TOOLS
    except Exception as e:
        print(f"[init] failed to load tools: {type(e).__name__}: {e}")
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
    history: str = ""   # JSON string of chat history
    command: str = ""   # "app" | "issue" | "research" | "chat" | ""


class TaskSyncRequest(BaseModel):
    action: str          # "build" | "msg"
    repo: str = ""
    issue_number: int = 0
    message: str = ""
    chat_id: str = ""


class TriggerRequest(BaseModel):
    prompt: str
    callback_repo: str = ""
    callback_workflow: str = ""
    context: str = ""


class ImplementRequest(BaseModel):
    repo: str               # e.g. "aw-apps/my-app"
    action: str             # "implement" | "fix-pr" | "review"
    issue_number: int = 0
    pr_number: int = 0
    notify_repo: str = ""   # e.g. "yazelin/byok-tg-main"
    notify_chat_id: str = ""


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
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
                headers={"X-Secret": CALLBACK_TOKEN},
                timeout=5,
            )
    except Exception:
        pass


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
    proc = await asyncio.create_subprocess_exec(
        "gh", "issue", "comment", str(issue_number),
        "--repo", repo, "--body", f"📝 User instruction:\n\n{message}",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()
    for label in ["agent-stuck", "needs-human-review"]:
        proc = await asyncio.create_subprocess_exec(
            "gh", "issue", "edit", str(issue_number),
            "--repo", repo, "--remove-label", label,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()
    proc = await asyncio.create_subprocess_exec(
        "gh", "workflow", "run", "implement.yml", "--repo", repo,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.communicate()
    return f"📝 已將指示傳達給 {repo} #{issue_number}"


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
    print(f"[task] accepted")  # No PII in public logs
    return {"status": "accepted"}


@app.post("/trigger")
async def trigger(req: TriggerRequest, x_api_key: str = Header(...)):
    if x_api_key != RUNNER_API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
    task_id = f"trg-{int(time.time())}"
    asyncio.create_task(_process_trigger(req, task_id))
    print(f"[trigger] accepted task_id={task_id}")
    return {"status": "accepted", "task_id": task_id}


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


@app.post("/implement")
async def implement(req: ImplementRequest, x_api_key: str = Header(...)):
    if x_api_key != RUNNER_API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
    task_id = f"impl-{int(time.time())}"
    asyncio.create_task(_process_implement(req, task_id))
    print(f"[implement] accepted task_id={task_id} repo={req.repo} action={req.action}")
    return {"status": "accepted", "task_id": task_id}


# --- Background processors ---

async def _process_task(req: TaskRequest) -> None:
    """Process a Telegram message."""
    try:
        # Build prompt with history context
        prompt = req.text
        if req.history:
            try:
                history_items = json.loads(req.history)
                if isinstance(history_items, list) and history_items:
                    lines = []
                    for item in history_items:
                        role = item.get("role", "unknown")
                        text = item.get("text", "")
                        lines.append(f"[{role}]: {text}")
                    history_text = "\n".join(lines)
                    prompt = (
                        f"--- Chat History ---\n{history_text}\n"
                        f"--- End History ---\n\n{req.text}"
                    )
            except (json.JSONDecodeError, TypeError):
                pass  # Ignore malformed history

        reply = await run_copilot_sdk(prompt)
        await send_telegram(req.chat_id, reply)
        await post_callback(req.chat_id, reply)
        print(f"[task] completed")
        # Log to private repo
        await log_to_private_repo(
            f"[tg] {datetime.now(timezone.utc).strftime('%H:%M')}",
            f"**User:** {req.text}\n\n**Assistant:** {reply}",
        )
    except Exception as e:
        print(f"[task] error err={type(e).__name__}")
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


# --- Implement helpers ---

async def _read_gh_output(*args) -> str:
    """Run gh command and return stdout."""
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return stdout.decode().strip()


async def _notify_telegram(notify_repo: str, chat_id: str, text: str) -> None:
    """Notify via Telegram through byok-tg-main notify.yml workflow."""
    if not notify_repo or not chat_id:
        return
    try:
        proc = await asyncio.create_subprocess_exec(
            "gh", "workflow", "run", "notify.yml",
            "--repo", notify_repo,
            "-f", f"chat_id={chat_id}",
            "-f", f"text={text}",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
    except Exception:
        pass


async def _push_changes(req: ImplementRequest, tmpdir: str) -> bool:
    """Stage, commit, and push changes. Returns True if changes were pushed."""
    proc = await asyncio.create_subprocess_exec(
        "git", "status", "--porcelain",
        cwd=tmpdir, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    if not stdout.decode().strip():
        print(f"[implement] no changes to push for {req.repo}")
        return False

    for cmd in [
        ["git", "add", "-A"],
        ["git", "commit", "-m", f"feat: implement #{req.issue_number or req.pr_number} via AI"],
        ["git", "push", "-u", "origin", "HEAD"],
    ]:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=tmpdir,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            print(f"[implement] git cmd failed: {cmd} err={err.decode()[:300]}")
            return False
    return True


async def _submit_review(req: ImplementRequest, reply: str) -> None:
    """Submit a PR review based on the AI response."""
    if "APPROVE" in reply:
        # Approve and merge
        await _read_gh_output(
            "gh", "pr", "review", str(req.pr_number),
            "--repo", req.repo, "--approve", "--body", reply,
        )
        await _read_gh_output(
            "gh", "pr", "merge", str(req.pr_number),
            "--repo", req.repo, "--squash", "--delete-branch",
        )
    else:
        # Request changes
        body = reply.replace("REQUEST_CHANGES:", "").strip() if "REQUEST_CHANGES:" in reply else reply
        await _read_gh_output(
            "gh", "pr", "review", str(req.pr_number),
            "--repo", req.repo, "--request-changes", "--body", body,
        )


async def _build_implement_prompt(req: ImplementRequest, tmpdir: str) -> str:
    """Build prompt for implementing an issue."""
    issue_text = await _read_gh_output(
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
        prompt += f"""
AGENTS.md (project conventions):
{agents_md}
"""
    prompt += f"""
INSTRUCTIONS:
1. Create and checkout branch: {branch}
2. Read the codebase to understand the project structure.
3. Follow the Approach / steps described in the issue.
4. Implement all required changes.
5. Commit your changes with a descriptive message.
6. Push the branch: git push -u origin {branch}
7. Create a PR with: gh pr create --repo {req.repo} --title "Implement #{req.issue_number}" --body "Closes #{req.issue_number}" --head {branch}

Work carefully and make sure code compiles / is valid before committing.
"""
    return prompt


async def _build_fix_pr_prompt(req: ImplementRequest, tmpdir: str) -> str:
    """Build prompt for fixing PR review comments."""
    comments = await _read_gh_output(
        "gh", "pr", "view", str(req.pr_number),
        "--repo", req.repo, "--comments",
    )

    branch = await _read_gh_output(
        "gh", "pr", "view", str(req.pr_number),
        "--repo", req.repo,
        "--json", "headRefName", "--jq", ".headRefName",
    )

    # Checkout PR branch
    proc = await asyncio.create_subprocess_exec(
        "git", "checkout", branch,
        cwd=tmpdir, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()

    prompt = f"""You are working in a cloned repo at {tmpdir}, on branch {branch}.

PR #{req.pr_number} REVIEW COMMENTS:
{comments}

INSTRUCTIONS:
1. Read the review comments above carefully.
2. Fix all issues mentioned in the review.
3. Commit your fixes with a descriptive message.
4. Push the changes: git push origin {branch}
"""
    return prompt


async def _build_review_prompt(req: ImplementRequest, tmpdir: str) -> str:
    """Build prompt for reviewing a PR."""
    diff = await _read_gh_output(
        "gh", "pr", "diff", str(req.pr_number), "--repo", req.repo,
    )
    # Truncate large diffs
    if len(diff) > 10000:
        diff = diff[:10000] + "\n... (truncated)"

    pr_body = await _read_gh_output(
        "gh", "pr", "view", str(req.pr_number),
        "--repo", req.repo,
        "--json", "body", "--jq", ".body",
    )

    prompt = f"""You are reviewing PR #{req.pr_number} in repo {req.repo}.

PR DESCRIPTION:
{pr_body}

PR DIFF:
{diff}

INSTRUCTIONS:
Review this PR for correctness, code quality, and potential issues.
Respond with EXACTLY one of:
- "APPROVE: <brief explanation>" if the PR looks good
- "REQUEST_CHANGES: <detailed explanation of what needs to be fixed>"

Be concise but thorough.
"""
    return prompt


async def _process_implement(req: ImplementRequest, task_id: str) -> None:
    """Clone repo, run AI, push changes."""
    tmpdir = tempfile.mkdtemp(prefix="impl-")
    try:
        print(f"[implement] starting task_id={task_id} repo={req.repo} action={req.action}")

        # Clone the repo
        clone_url = f"https://x-access-token:{GH_PAT}@github.com/{req.repo}.git"
        proc = await asyncio.create_subprocess_exec(
            "git", "clone", clone_url, tmpdir,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"git clone failed: {stderr.decode()[:300]}")

        # Configure git
        for cmd in [
            ["git", "config", "user.name", "ai-bot"],
            ["git", "config", "user.email", "ai-bot@users.noreply.github.com"],
        ]:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=tmpdir,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.communicate()

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
        print(f"[implement] AI done task_id={task_id} action={req.action}")

        # Post-process based on action
        if req.action in ("implement", "fix-pr"):
            pushed = await _push_changes(req, tmpdir)
            status_msg = "pushed" if pushed else "no changes"
            await _notify_telegram(
                req.notify_repo, req.notify_chat_id,
                f"✅ {req.action} done for {req.repo} #{req.issue_number or req.pr_number} ({status_msg})",
            )
        elif req.action == "review":
            await _submit_review(req, reply)
            await _notify_telegram(
                req.notify_repo, req.notify_chat_id,
                f"✅ review done for {req.repo} PR #{req.pr_number}",
            )

        print(f"[implement] completed task_id={task_id}")

    except Exception as e:
        print(f"[implement] error task_id={task_id} err={type(e).__name__}: {e}")
        # Add agent-stuck label on failure
        try:
            ref = req.issue_number or req.pr_number
            if ref:
                await _read_gh_output(
                    "gh", "issue", "edit", str(ref),
                    "--repo", req.repo, "--add-label", "agent-stuck",
                )
        except Exception:
            pass
        await _notify_telegram(
            req.notify_repo, req.notify_chat_id,
            f"❌ {req.action} failed for {req.repo} #{req.issue_number or req.pr_number}: {e}",
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
