"""Registered local projects and read-only CodeBuddy native message collection."""

import json
import logging
from pathlib import Path

from .codebuddy_history import discover_history
from .identity import resolve_project
from .messages import publish
from .storage import connect, init_local

LOG = logging.getLogger(__name__)


def registrations(db_path: str) -> list[dict]:
    with connect(db_path) as db:
        init_local(db)
        db.execute("CREATE TABLE IF NOT EXISTS watched_projects "
                   "(root TEXT PRIMARY KEY, project_id TEXT NOT NULL, codebuddy_history TEXT NOT NULL)")
        return [dict(row) for row in db.execute("SELECT * FROM watched_projects")]


def register(db_path: str, path: str, history_dir: str = "") -> dict:
    project = resolve_project(path)
    if not project:
        raise ValueError("Not a Git project")
    registrations(db_path)
    if history_dir == "auto":
        history_dir = str(discover_history(project.root))
    elif history_dir:
        history_dir = str(Path(history_dir).expanduser().resolve())
    if history_dir and not (Path(history_dir) / "index.json").is_file():
        raise ValueError("CodeBuddy history index.json not found")
    with connect(db_path) as db:
        db.execute("INSERT INTO watched_projects VALUES (?, ?, ?) ON CONFLICT(root) DO UPDATE SET "
                   "project_id=excluded.project_id, codebuddy_history=excluded.codebuddy_history",
                   (project.root, project.project_id, history_dir))
    return dict(root=project.root, project_id=project.project_id, codebuddy_history=history_dir)


def child(root: Path, identifier: str) -> Path:
    if not isinstance(identifier, str) or not identifier or identifier in (".", "..") or any(
            char in identifier for char in '/\\:'):
        raise ValueError("invalid native history ID")
    result = (root / identifier).resolve()
    if root.resolve() not in result.parents:
        raise ValueError("native history path escapes its directory")
    return result


def scan_codebuddy(db_path: str, project: dict) -> int:
    """Read completed native messages, including new replies to imported snapshots."""
    root = Path(project["codebuddy_history"])
    index = json.loads((root / "index.json").read_text(encoding="utf-8"))
    count = 0
    for conversation in index["conversations"]:
        try:
            folder = child(root, conversation["id"])
            messages = json.loads((folder / "index.json").read_text(encoding="utf-8"))["messages"]
            for sequence, entry in enumerate(messages):
                if not entry.get("isComplete") or entry.get("role") not in ("user", "assistant"):
                    continue
                message_path = child(folder / "messages", entry["id"] + ".json")
                message = json.loads(message_path.read_text(encoding="utf-8"))
                extra = message.get("extra") or {}
                extra = json.loads(extra) if isinstance(extra, str) else extra
                if isinstance(extra, dict) and extra.get("agentsync"):
                    continue  # Imported messages are already known; collect only native continuations.
                payload = message["message"]
                payload = json.loads(payload) if isinstance(payload, str) else payload
                parts = payload.get("content", [])
                content = parts if isinstance(parts, str) else "\n".join(
                    part["text"] for part in parts if part.get("type") == "text" and isinstance(part.get("text"), str))
                if not content.strip():
                    continue
                result = publish(db_path, project["root"], "codebuddy", conversation["id"],
                                 entry["id"], entry["role"], content, message.get("createdAt", ""), sequence)
                count += result["status"] == "created"
        except (OSError, ValueError, KeyError, TypeError) as error:
            LOG.warning("CodeBuddy conversation postponed: %s", error)
    return count


def scan_registered(db_path: str) -> int:
    count = 0
    for project in registrations(db_path):
        if project["codebuddy_history"]:
            try:
                count += scan_codebuddy(db_path, project)
            except (OSError, ValueError, KeyError, TypeError) as error:
                LOG.warning("CodeBuddy scan postponed: %s", error)
    return count
