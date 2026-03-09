"""Async helpers for managing child app repos.

Called by Copilot SDK tools during /app processing:
  create_repo  — create a public repo under APPS_ORG
  setup_repo   — clone, write files, push, enable GitHub Pages
  create_issues — create issues with copilot-task label
  setup_secrets — set required secrets on child repo
"""

import asyncio
import os
import shutil
import tempfile

GH_PAT = os.environ.get("GH_TOKEN", "")
APPS_ORG = os.environ.get("APPS_ORG", "aw-apps")


async def _run(cmd, **kwargs):
    """Run a subprocess, return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **kwargs,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode(), stderr.decode()


async def create_repo(name: str, description: str) -> dict:
    """Create a public repo under APPS_ORG."""
    full_name = f"{APPS_ORG}/{name}"
    rc, stdout, stderr = await _run([
        "gh", "repo", "create", full_name,
        "--public",
        "--description", description,
        "--clone=false",
    ])
    if rc != 0:
        return {"ok": False, "error": stderr.strip() or stdout.strip()}
    return {
        "ok": True,
        "repo": full_name,
        "url": f"https://github.com/{full_name}",
    }


async def setup_repo(repo: str, files: list[dict]) -> dict:
    """Clone repo, write files, push, enable GitHub Pages.

    Args:
        repo:  full name e.g. "aw-apps/my-app"
        files: list of {"path": "...", "content": "..."} dicts
    """
    tmpdir = tempfile.mkdtemp(prefix="app-setup-")
    try:
        # Clone with retries
        clone_url = f"https://x-access-token:{GH_PAT}@github.com/{repo}.git"
        cloned = False
        for attempt in range(3):
            rc, _, stderr = await _run(["git", "clone", clone_url, tmpdir])
            if rc == 0:
                cloned = True
                break
            # Clean dir for retry (git clone expects empty target)
            shutil.rmtree(tmpdir, ignore_errors=True)
            os.makedirs(tmpdir, exist_ok=True)
            if attempt < 2:
                await asyncio.sleep(5)
        if not cloned:
            return {"ok": False, "error": f"git clone failed after 3 attempts: {stderr.strip()}"}

        # Write files
        for f in files:
            fpath = os.path.join(tmpdir, f["path"])
            os.makedirs(os.path.dirname(fpath), exist_ok=True)
            with open(fpath, "w") as fh:
                fh.write(f["content"])

        # Auto-inject workflow templates (overwrite any AI-generated ones)
        notify_repo = os.environ.get("NOTIFY_REPO", "yazelin/byok-tg-main")
        notify_chat_id = os.environ.get("NOTIFY_CHAT_ID", "")
        templates_dir = os.path.join(os.path.dirname(__file__), "..", "templates", "workflows")
        if os.path.isdir(templates_dir):
            wf_dir = os.path.join(tmpdir, ".github", "workflows")
            os.makedirs(wf_dir, exist_ok=True)
            for tmpl_name in os.listdir(templates_dir):
                if not tmpl_name.endswith(".yml"):
                    continue
                with open(os.path.join(templates_dir, tmpl_name)) as tf:
                    content = tf.read()
                content = content.replace("PLACEHOLDER_NOTIFY_REPO", notify_repo)
                content = content.replace("PLACEHOLDER_CHAT_ID", notify_chat_id)
                with open(os.path.join(wf_dir, tmpl_name), "w") as wf:
                    wf.write(content)

        # Set remote URL with PAT
        await _run(
            ["git", "remote", "set-url", "origin", clone_url],
            cwd=tmpdir,
        )

        # Configure git user
        await _run(["git", "config", "user.name", "ai-bot"], cwd=tmpdir)
        await _run(["git", "config", "user.email", "ai-bot@users.noreply.github.com"], cwd=tmpdir)

        # Get default branch
        rc, branch_out, _ = await _run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=tmpdir,
        )
        default_branch = branch_out.strip() or "main"

        # Add, commit, push
        await _run(["git", "add", "-A"], cwd=tmpdir)
        rc, _, stderr = await _run(
            ["git", "commit", "-m", "Initial app scaffold"],
            cwd=tmpdir,
        )
        if rc != 0:
            return {"ok": False, "error": f"git commit failed: {stderr.strip()}"}

        rc, _, stderr = await _run(
            ["git", "push", "-u", "origin", default_branch],
            cwd=tmpdir,
        )
        if rc != 0:
            return {"ok": False, "error": f"git push failed: {stderr.strip()}"}

        # Enable GitHub Pages with retries
        org = repo.split("/")[0]
        name = repo.split("/")[1]
        pages_enabled = False
        for attempt in range(3):
            rc, _, stderr = await _run([
                "gh", "api", f"repos/{repo}/pages",
                "-X", "POST",
                "-f", f"source[branch]={default_branch}",
                "-f", "source[path]=/",
            ])
            if rc == 0:
                pages_enabled = True
                break
            if attempt < 2:
                await asyncio.sleep(5)

        # Set homepage URL
        homepage = f"https://{org}.github.io/{name}/"
        await _run([
            "gh", "api", f"repos/{repo}",
            "-X", "PATCH",
            "-f", f"homepage={homepage}",
        ])

        return {
            "ok": True,
            "files_pushed": len(files),
            "pages_enabled": pages_enabled,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


async def create_issues(repo: str, issues: list[dict]) -> dict:
    """Create issues with copilot-task label.

    Args:
        repo:   full name e.g. "aw-apps/my-app"
        issues: list of {"title": "...", "body": "..."} dicts
    """
    try:
        # Create labels
        labels = [
            ("copilot-task", "0e8a16", "Task for Copilot agent"),
            ("agent-stuck", "d93f0b", "Agent needs help"),
            ("needs-human-review", "fbca04", "Needs human review"),
        ]
        for label_name, color, desc in labels:
            await _run([
                "gh", "label", "create", label_name,
                "--repo", repo,
                "--color", color,
                "--description", desc,
                "--force",
            ])

        # Create issues
        numbers = []
        for issue in issues:
            rc, stdout, stderr = await _run([
                "gh", "issue", "create",
                "--repo", repo,
                "--title", issue["title"],
                "--body", issue["body"],
                "--label", "copilot-task",
            ])
            if rc != 0:
                return {
                    "ok": False,
                    "error": f"Failed to create issue '{issue['title']}': {stderr.strip()}",
                }
            # Parse issue number from URL (e.g. https://github.com/org/repo/issues/1)
            url = stdout.strip()
            try:
                num = int(url.rstrip("/").split("/")[-1])
                numbers.append(num)
            except (ValueError, IndexError):
                numbers.append(0)

        return {"ok": True, "issues_created": len(numbers), "numbers": numbers}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def setup_secrets(repo: str) -> dict:
    """Set required secrets on child repo."""
    try:
        runner_api_key = os.environ.get("RUNNER_API_KEY", "")
        callback_url = os.environ.get("CALLBACK_URL", "")
        callback_token = os.environ.get("CALLBACK_TOKEN", "")
        notify_chat_id = os.environ.get("NOTIFY_CHAT_ID", "")
        notify_repo = os.environ.get("NOTIFY_REPO", "yazelin/byok-tg-main")

        # Derive RUNNER_URL from CALLBACK_URL (strip /api/callback suffix)
        runner_url = callback_url
        if runner_url.endswith("/api/callback"):
            runner_url = runner_url[: -len("/api/callback")]

        secrets = {
            "RUNNER_API_KEY": runner_api_key,
            "RUNNER_URL": runner_url,
            "NOTIFY_REPO": notify_repo,
            "NOTIFY_CHAT_ID": notify_chat_id,
            "GH_TOKEN": GH_PAT,
        }

        count = 0
        for name, value in secrets.items():
            rc, _, stderr = await _run([
                "gh", "secret", "set", name,
                "--repo", repo,
                "--body", value,
            ])
            if rc != 0:
                return {"ok": False, "error": f"Failed to set secret {name}: {stderr.strip()}"}
            count += 1

        return {"ok": True, "secrets_set": count}
    except Exception as e:
        return {"ok": False, "error": str(e)}
