"""Shell and file tools for Copilot SDK sessions.

These give the AI full shell access within a working directory,
similar to Copilot CLI's --autopilot --yolo mode.
"""

import asyncio
import json
import os

from copilot import define_tool
from pydantic import BaseModel, Field


class RunCommandParams(BaseModel):
    command: str = Field(description="Shell command to execute (bash)")
    working_dir: str = Field(default="", description="Working directory (defaults to repo root)")


class ReadFileParams(BaseModel):
    path: str = Field(description="File path relative to repo root")


class WriteFileParams(BaseModel):
    path: str = Field(description="File path relative to repo root")
    content: str = Field(description="File content to write")


class ListDirectoryParams(BaseModel):
    path: str = Field(default=".", description="Directory path relative to repo root")


def create_shell_tools(repo_dir: str) -> list:
    """Create shell/file tools scoped to a specific repo directory."""

    @define_tool(description="Execute a shell command in the repo. Use for git, build tools, tests, etc.")
    async def run_command(params: RunCommandParams) -> str:
        cwd = params.working_dir or repo_dir
        # Ensure cwd is within repo_dir
        cwd_abs = os.path.abspath(os.path.join(repo_dir, cwd)) if not os.path.isabs(cwd) else cwd
        if not cwd_abs.startswith(repo_dir):
            cwd_abs = repo_dir
        try:
            proc = await asyncio.create_subprocess_shell(
                params.command,
                cwd=cwd_abs,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            output = stdout.decode(errors="replace")
            err_output = stderr.decode(errors="replace")
            result = {"exit_code": proc.returncode, "stdout": output[-3000:], "stderr": err_output[-1000:]}
        except asyncio.TimeoutError:
            result = {"exit_code": -1, "stdout": "", "stderr": "Command timed out (120s)"}
        except Exception as e:
            result = {"exit_code": -1, "stdout": "", "stderr": str(e)}
        return json.dumps(result)

    @define_tool(description="Read a file from the repo.")
    async def read_file(params: ReadFileParams) -> str:
        filepath = os.path.join(repo_dir, params.path)
        filepath = os.path.abspath(filepath)
        if not filepath.startswith(repo_dir):
            return json.dumps({"error": "Path outside repo"})
        try:
            with open(filepath, "r", errors="replace") as f:
                content = f.read()
            if len(content) > 10000:
                content = content[:10000] + "\n... (truncated)"
            return json.dumps({"path": params.path, "content": content})
        except FileNotFoundError:
            return json.dumps({"error": f"File not found: {params.path}"})
        except Exception as e:
            return json.dumps({"error": str(e)})

    @define_tool(description="Write or create a file in the repo.")
    async def write_file(params: WriteFileParams) -> str:
        filepath = os.path.join(repo_dir, params.path)
        filepath = os.path.abspath(filepath)
        if not filepath.startswith(repo_dir):
            return json.dumps({"error": "Path outside repo"})
        try:
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            with open(filepath, "w") as f:
                f.write(params.content)
            return json.dumps({"ok": True, "path": params.path, "bytes": len(params.content)})
        except Exception as e:
            return json.dumps({"error": str(e)})

    @define_tool(description="List files and directories in the repo.")
    async def list_directory(params: ListDirectoryParams) -> str:
        dirpath = os.path.join(repo_dir, params.path)
        dirpath = os.path.abspath(dirpath)
        if not dirpath.startswith(repo_dir):
            return json.dumps({"error": "Path outside repo"})
        try:
            entries = []
            for entry in sorted(os.listdir(dirpath)):
                full = os.path.join(dirpath, entry)
                entries.append({
                    "name": entry,
                    "type": "dir" if os.path.isdir(full) else "file",
                })
            return json.dumps({"path": params.path, "entries": entries})
        except Exception as e:
            return json.dumps({"error": str(e)})

    return [run_command, read_file, write_file, list_directory]
