"""Portable, source-identified messages shared by local IDEs and Cloud."""

import hashlib
import json
from datetime import datetime, timezone

from .identity import resolve_project
from .storage import connect, init_local, refresh_handoff, save_event


def validate_event(event: dict) -> None:
    if not isinstance(event, dict):
        raise ValueError("expected event object")
    for key in ("event_id", "project_id", "task_id", "branch", "role", "content",
                "raw_record", "phase", "source_timestamp"):
        if not isinstance(event.get(key), str):
            raise ValueError("invalid event field: " + key)
    if not all(event[key] for key in ("event_id", "project_id", "task_id", "content")):
        raise ValueError("empty event identity or content")
    if event["role"] not in ("user", "assistant"):
        raise ValueError("invalid role")
    if type(event.get("source_offset")) is not int or event["source_offset"] < 0:
        raise ValueError("invalid source_offset")


def publish(db_path: str, path: str, source: str, task_id: str, message_id: str,
            role: str, content: str, timestamp: str = "", sequence: int = 0) -> dict:
    """Publish one message; retries with the same identity/content are idempotent."""
    if source not in ("trae", "codebuddy", "codex"):
        raise ValueError("source must be trae, codebuddy or codex")
    if not all(isinstance(value, str) and value for value in (task_id, message_id, content)):
        raise ValueError("task_id, message_id and content are required")
    project = resolve_project(path)
    if not project:
        raise ValueError("Not a Git project")
    stamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00")) if timestamp else datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    task = source + ":" + hashlib.sha256((project.project_id + ":" + task_id).encode()).hexdigest()
    seed = json.dumps([task, message_id, role, content], ensure_ascii=False)
    event = dict(event_id=hashlib.sha256(seed.encode()).hexdigest(), project_id=project.project_id,
                 task_id=task, branch=project.branch, role=role, content=content,
                 raw_record=json.dumps({"source": source, "task_id": task_id, "message_id": message_id}),
                 phase="final", source_offset=sequence,
                 source_timestamp=stamp.astimezone(timezone.utc).isoformat())
    validate_event(event)
    with connect(db_path) as db:
        init_local(db)
        previous = db.execute("SELECT branch FROM events WHERE task_id=? LIMIT 1", (task,)).fetchone()
        if previous:
            event["branch"] = previous["branch"]
        inserted = save_event(db, event)
        if inserted:
            refresh_handoff(db, task, project.project_id, event["branch"], project.head)
    return {"status": "created" if inserted else "unchanged", "event_id": event["event_id"], "task_id": task}


def changes(db, project_id: str, after: int = 0, limit: int = 100) -> dict:
    """Return an ascending, project-filtered page using database insertion cursors."""
    if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("after must be nonnegative and limit must be 1..500")
    rows = db.execute("SELECT rowid AS cursor, * FROM events WHERE project_id=? AND rowid>? "
                      "ORDER BY rowid LIMIT ?", (project_id, after, limit + 1)).fetchall()
    page = [dict(row) for row in rows[:limit]]
    return {"events": page, "next_cursor": page[-1]["cursor"] if page else after,
            "has_more": len(rows) > limit}
