"""Custom tools for Copilot SDK sessions.

This file is the public repo default. The private repo (byok-tg-main)
can override it by placing its own tools/ directory.
"""

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
