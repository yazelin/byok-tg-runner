"""Custom tools for Copilot SDK sessions.

This file is the public repo default. The private repo (byok-tg-main)
can override it by placing its own tools/ directory.
"""

import asyncio
import json
import os

from copilot import define_tool
from pydantic import BaseModel, Field


# --- Parameter Models ---

class CreateRepoParams(BaseModel):
    name: str = Field(description="Repository name (e.g. 'my-app')")
    description: str = Field(description="Repository description")


class SetupRepoParams(BaseModel):
    repo: str = Field(description="Full repo name (e.g. 'aw-apps/my-app')")
    files_json: str = Field(description="JSON array of {path, content} objects")


class CreateIssuesParams(BaseModel):
    repo: str = Field(description="Full repo name (e.g. 'aw-apps/my-app')")
    issues_json: str = Field(description="JSON array of {title, body} objects")


class SetupSecretsParams(BaseModel):
    repo: str = Field(description="Full repo name (e.g. 'aw-apps/my-app')")


class TriggerWorkflowParams(BaseModel):
    repo: str = Field(description="Full repo name (e.g. 'aw-apps/my-app')")
    workflow: str = Field(description="Workflow file name (e.g. 'implement.yml')")


class SendTelegramParams(BaseModel):
    chat_id: str = Field(description="Telegram chat ID")
    text: str = Field(description="Message text")


class GetWeatherParams(BaseModel):
    city: str = Field(description="City name")


# --- Tools ---

@define_tool(description="Create a new public GitHub repository under the aw-apps organization. Returns {ok, repo, url} or {ok: false, error}.")
async def create_repo(params: CreateRepoParams) -> str:
    from server.app_factory import create_repo as _create_repo
    result = await _create_repo(params.name, params.description)
    return json.dumps(result)


@define_tool(description="Clone a repo, write files to it, push, and enable GitHub Pages. files_json is a JSON array of {path, content} objects.")
async def setup_repo(params: SetupRepoParams) -> str:
    from server.app_factory import setup_repo as _setup_repo
    files = json.loads(params.files_json)
    result = await _setup_repo(params.repo, files)
    return json.dumps(result)


@define_tool(description="Create issues with copilot-task label in a repository. issues_json is a JSON array of {title, body} objects.")
async def create_issues(params: CreateIssuesParams) -> str:
    from server.app_factory import create_issues as _create_issues
    issues = json.loads(params.issues_json)
    result = await _create_issues(params.repo, issues)
    return json.dumps(result)


@define_tool(description="Set required secrets (RUNNER_API_KEY, RUNNER_URL, etc.) on a child repository.")
async def setup_secrets(params: SetupSecretsParams) -> str:
    from server.app_factory import setup_secrets as _setup_secrets
    result = await _setup_secrets(params.repo)
    return json.dumps(result)


@define_tool(description="Trigger a workflow_dispatch on a GitHub repository.")
async def trigger_workflow(params: TriggerWorkflowParams) -> str:
    proc = await asyncio.create_subprocess_exec(
        "gh", "workflow", "run", params.workflow, "--repo", params.repo,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        return json.dumps({"ok": False, "error": stderr.decode().strip()[-300:]})
    return json.dumps({"ok": True})


@define_tool(description="Send a text message to a Telegram chat.")
async def send_telegram_message(params: SendTelegramParams) -> str:
    import httpx
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    async with httpx.AsyncClient() as http:
        for i in range(0, len(params.text), 4096):
            await http.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": params.chat_id, "text": params.text[i:i + 4096]},
                timeout=10,
            )
    return json.dumps({"ok": True})


@define_tool(description="Get current weather for a city (demo tool).")
async def get_weather(params: GetWeatherParams) -> str:
    weather = {"Taiwan": "28C sunny", "Tokyo": "22C cloudy", "New York": "15C rainy",
               "Taipei": "28C sunny", "taipei": "28C sunny"}
    return json.dumps({"city": params.city, "weather": weather.get(params.city, "Unknown city")})


ALL_TOOLS = [
    create_repo,
    setup_repo,
    create_issues,
    setup_secrets,
    trigger_workflow,
    send_telegram_message,
    get_weather,
]
