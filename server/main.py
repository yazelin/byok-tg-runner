import asyncio
import json
import logging
import os
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
