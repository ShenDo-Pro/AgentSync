"""Read append-only Codex rollout files without changing native sessions."""

import hashlib
import json
from pathlib import Path
from typing import Iterator


def rollout_files(codex_home: Path) -> Iterator[Path]:
    """Yield current and archived Codex JSONL files."""
    for folder in (codex_home / "sessions", codex_home / "archived_sessions"):
        if folder.is_dir():
            yield from folder.rglob("rollout-*.jsonl")


def session_metadata(path: Path) -> dict | None:
    """Read the initial session metadata record."""
    try:
        with path.open("rb") as stream:
            line = stream.readline()
        item = json.loads(line)
        if item.get("type") == "session_meta":
            return item.get("payload") or None
    except (OSError, ValueError, TypeError):
        pass
    return None


def new_lines(path: Path, offset: int) -> tuple[list[tuple[int, dict]], int]:
    """Read complete new lines and retain the trailing partial line."""
    records = []
    with path.open("rb") as stream:
        stream.seek(offset)
        while True:
            start = stream.tell()
            line = stream.readline()
            if not line or not line.endswith(b"\n"):
                stream.seek(start)
                break
            try:
                records.append((start, json.loads(line)))
            except (UnicodeDecodeError, ValueError):
                continue
        next_offset = stream.tell()
    return records, next_offset


def dialogue_event(item: dict, task_id: str, source_offset: int) -> dict | None:
    """Extract user and assistant dialogue from one rollout record."""
    if item.get("type") != "response_item":
        return None
    payload = item.get("payload") or {}
    if payload.get("type") != "message":
        return None
    role = payload.get("role")
    if role not in ("user", "assistant"):
        return None
    phase = payload.get("phase") or ""
    if role == "assistant" and phase not in ("final_answer", "final", ""):
        return None
    parts = []
    for content in payload.get("content") or []:
        if content.get("type") in ("input_text", "output_text", "text"):
            value = content.get("text")
            if isinstance(value, str):
                parts.append(value)
    content = "\n".join(parts).strip()
    if not content:
        return None
    seed = "%s:%d:%s:%s" % (task_id, source_offset, role, content)
    event_id = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return {
        "event_id": event_id,
        "task_id": task_id,
        "role": role,
        "content": content,
        "raw_record": json.dumps(item, ensure_ascii=False, separators=(",", ":")),
        "phase": phase,
        "source_offset": source_offset,
        "source_timestamp": item.get("timestamp") or "",
    }
