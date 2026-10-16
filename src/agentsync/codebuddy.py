"""Inject a Codex task handoff at CodeBuddy session start."""

import json
import os
import shlex
import sys
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .identity import resolve_project
from .storage import connect, init_local, select_handoff


def _cloud_handoff(cloud_url: str, token: str, project_id: str,
                   branch: str, task_id: str) -> dict:
    query = urlencode({"project_id": project_id, "branch": branch,
                       "task_id": task_id})
    request = Request(cloud_url.rstrip("/") + "/v1/handoff?" + query,
                      headers={"Authorization": "Bearer " + token})
    with urlopen(request, timeout=3) as response:
        return json.load(response)


def handoff(cwd: str, db_path: str, cloud_url: str = "",
            token: str = "", task_id: str = "") -> dict:
    """Find a unique Codex task for the current Git branch."""
    project = resolve_project(cwd)
    if not project:
        return {"status": "missing", "reason": "not a Git project"}
    if cloud_url and token:
        try:
            return _cloud_handoff(cloud_url, token, project.project_id,
                                  project.branch, task_id)
        except (OSError, ValueError):
            pass
    with connect(db_path) as db:
        init_local(db)
        return select_handoff(db, project.project_id, project.branch, task_id)


def hook_main() -> None:
    """Read CodeBuddy Hook input and emit session context JSON."""
    try:
        data = json.load(sys.stdin)
        cwd = data.get("cwd") or os.environ.get("CODEBUDDY_PROJECT_DIR") or os.getcwd()
        result = handoff(cwd, os.environ.get("AGENTSYNC_DB", "~/.agentsync/local.db"),
                         os.environ.get("AGENTSYNC_CLOUD_URL", ""),
                         os.environ.get("AGENTSYNC_TOKEN", ""),
                         os.environ.get("AGENTSYNC_TASK_ID", ""))
        if result.get("status") == "ready":
            item = result["handoff"]
            context = ("AgentSync Codex 接力上下文\n"
                       "任务 ID：%s\n项目分支：%s\n当前 Git HEAD：%s\n来源事件：%s\n\n%s" %
                       (item["task_id"], item["branch"],
                        resolve_project(cwd).head,
                        ", ".join(item["source_event_ids"]), item["summary"]))
            print(json.dumps({"continue": True, "hookSpecificOutput": {
                "hookEventName": "SessionStart", "additionalContext": context}},
                ensure_ascii=False))
        else:
            print(json.dumps({"continue": True}))
            if result.get("status") == "ambiguous":
                print("AgentSync: multiple Codex tasks match this branch; set AGENTSYNC_TASK_ID to select one.",
                      file=sys.stderr)
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(json.dumps({"continue": True}))
        print("AgentSync hook: %s" % error, file=sys.stderr)


def install_hook(settings_path: Path | None = None) -> Path:
    """Add the global CodeBuddy SessionStart Hook once."""
    settings_path = settings_path or Path.home() / ".codebuddy" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    if settings_path.exists():
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        if not isinstance(settings, dict):
            raise ValueError("CodeBuddy settings must be a JSON object")
    else:
        settings = {}
    executable = sys.executable.replace("\\", "/")
    command = "%s hook" % shlex.quote(executable)
    if not getattr(sys, "frozen", False):
        command = "%s -m agentsync.cli hook" % shlex.quote(executable)
    entry = {"matcher": "startup", "hooks": [
        {"type": "command", "command": command, "timeout": 10}]}
    hooks = settings.setdefault("hooks", {})
    startup = hooks.setdefault("SessionStart", [])
    if not any("agentsync" in json.dumps(value).lower() for value in startup):
        startup.append(entry)
        temporary = settings_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
        temporary.replace(settings_path)
    return settings_path
