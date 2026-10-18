"""Import visible Codex dialogue as a CodeBuddyCN 3.10 native history snapshot.

Native history is only written with CodeBuddyCN closed. Imported snapshots are
immutable: a newer source snapshot gets a new ID, preserving native continuations.
"""

import csv
import hashlib
import io
import json
import ntpath
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone

from .codex import new_lines, session_metadata


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def _atomic(path: Path, value: object) -> None:
    temporary = path.with_name(path.name + ".agentsync.tmp")
    temporary.write_text(_json(value), encoding="utf-8")
    temporary.replace(path)


def codebuddy_running() -> bool:
    if os.name != "nt":
        raise ValueError("Native history import currently supports Windows CodeBuddyCN only")
    result = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq CodeBuddy CN.exe", "/FO", "CSV", "/NH"],
        capture_output=True, check=True, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    return any(row and row[0].lower() == "codebuddy cn.exe"
               for row in csv.reader(io.StringIO(result.stdout.decode("utf-8", errors="replace"))))


def discover_history(workspace: str, data_root: Path | None = None) -> Path:
    """Find an existing workspace store; never guess an account when ambiguous."""
    data_root = data_root or Path.home() / "AppData/Local/CodeBuddyExtension/Data"
    normalized = ntpath.normpath(workspace)
    variants = {normalized, normalized[:1].lower() + normalized[1:], normalized.lower()}
    hashes = {hashlib.md5(value.encode("utf-8")).hexdigest() for value in variants}
    candidates = {path.parent for digest in hashes
                  for path in data_root.glob("*/CodeBuddyIDE/*/history/" + digest + "/index.json")}
    if len(candidates) != 1:
        raise ValueError("Expected one CodeBuddyCN history store for this workspace; found %d. "
                         "Open the project once, or specify --history-dir." % len(candidates))
    return candidates.pop()


def build_snapshot(rollout: Path, name: str = "") -> dict:
    metadata = session_metadata(rollout)
    if not metadata or not metadata.get("id"):
        raise ValueError("Missing Codex session metadata")
    visible = []
    records, _ = new_lines(rollout, 0)
    for offset, item in records:
        if not isinstance(item, dict) or item.get("type") != "response_item":
            continue
        payload = item.get("payload") or {}
        role = payload.get("role")
        if payload.get("type") != "message" or role not in ("user", "assistant"):
            continue
        if role == "assistant" and payload.get("phase", "") not in ("", "final", "final_answer", "commentary"):
            continue
        content = "\n".join(part["text"] for part in payload.get("content", [])
                            if part.get("type") in ("input_text", "output_text", "text")
                            and isinstance(part.get("text"), str)).strip()
        if role == "user":
            for tag in ("environment_context", "external_codex_apps_open_page"):
                content = re.sub(r"<" + tag + r">[\s\S]*?</" + tag + r">", "", content).strip()
            if content.startswith("# AGENTS.md instructions for "):
                continue
        if not content:
            continue
        visible.append({"role": role, "text": content, "offset": offset,
                        "timestamp": item.get("timestamp") or metadata.get("timestamp") or ""})
    if not visible:
        raise ValueError("No visible dialogue found")
    digest = hashlib.sha256(_json({"task": metadata["id"], "messages": visible}).encode("utf-8")).hexdigest()
    # Keep native paths below Windows MAX_PATH even in the deep account directory.
    conversation_id = "codex-" + digest[:24]
    fallback_time = datetime.now(timezone.utc).isoformat()
    messages, indexes, requests = {}, [], []
    for event in visible:
        message_id = hashlib.sha256(
            (str(metadata["id"]) + ":" + str(event["offset"])).encode()).hexdigest()[:32]
        timestamp = event["timestamp"] or fallback_time
        if event["role"] == "user" or not requests:
            requests.append({"id": "request-" + message_id, "type": "craft", "messages": [],
                             "state": "complete", "startedAt": timestamp})
        requests[-1]["messages"].append(message_id)
        messages[message_id] = {
            "id": message_id, "role": event["role"], "status": "active", "createdAt": timestamp,
            "message": json.dumps({"role": event["role"], "content": [
                {"type": "text", "text": event["text"]}]}, ensure_ascii=False),
            "extra": json.dumps({"agentsync": {"source": "codex", "taskId": metadata["id"],
                                                  "sourceOffset": event["offset"]}}),
        }
        indexes.append({"id": message_id, "type": "text", "role": event["role"], "isComplete": True})
    title = name or next((e["text"][:60] for e in visible if e["role"] == "user"), "历史会话")
    conversation = {"id": conversation_id, "type": "craft", "name": "[Codex] " + title,
                    "createdAt": visible[0]["timestamp"] or fallback_time,
                    "lastMessageAt": visible[-1]["timestamp"] or fallback_time,
                    "originalId": metadata["id"]}
    return {"conversation": conversation, "index": {"messages": indexes, "requests": requests},
            "messages": messages, "source": {"tool": "codex", "task_id": metadata["id"],
            "workspace": metadata.get("cwd", ""), "digest": digest, "rollout": str(rollout)}}


def import_snapshot(snapshot: dict, history_dir: Path, backup_root: Path,
                    activate: bool = False) -> dict:
    """Append a snapshot and back up the complete store before changing its index."""
    if codebuddy_running():
        raise ValueError("Close CodeBuddyCN before importing native history to prevent cached writes")
    history_dir = history_dir.resolve()
    backup_root = backup_root.resolve()
    if backup_root == history_dir or history_dir in backup_root.parents:
        raise ValueError("Backup directory must be outside the native history directory")
    index_path = history_dir / "index.json"
    original = index_path.read_bytes()
    index = json.loads(original)
    if not isinstance(index.get("conversations"), list) or not isinstance(index.get("current"), str):
        raise ValueError("Unsupported CodeBuddy history index")
    conversation = snapshot["conversation"]
    conversation_id = conversation["id"]
    if not re.fullmatch(r"codex-[0-9a-f]{24}", conversation_id):
        raise ValueError("Invalid imported conversation ID")
    existing = next((entry for entry in index["conversations"] if entry.get("id") == conversation_id), None)
    if existing and not activate:
        return {"status": "unchanged", "conversation_id": conversation_id}
    destination = history_dir / conversation_id
    if destination.exists():
        source_path = destination / "agentsync-source.json"
        if not source_path.exists() or json.loads(source_path.read_text(encoding="utf-8")) != snapshot["source"]:
            raise ValueError("Conversation ID collision; refusing to overwrite native history")
    backup_root.mkdir(parents=True, exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix="codebuddy-history-", dir=backup_root)) / "history"
    shutil.copytree(history_dir, backup)
    if not destination.exists():
        with tempfile.TemporaryDirectory(prefix=".as-", dir=history_dir.parent) as staging:
            staged = Path(staging) / conversation_id
            (staged / "messages").mkdir(parents=True)
            for message_id, message in snapshot["messages"].items():
                if not re.fullmatch(r"[0-9a-f]{32}", message_id):
                    raise ValueError("Invalid message ID")
                (staged / "messages" / (message_id + ".json")).write_text(_json(message), encoding="utf-8")
            (staged / "index.json").write_text(_json(snapshot["index"]), encoding="utf-8")
            (staged / "agentsync-source.json").write_text(_json(snapshot["source"]), encoding="utf-8")
            staged.rename(destination)
    # Recheck before committing the global pointers; never clobber concurrent edits.
    if codebuddy_running() or index_path.read_bytes() != original:
        raise ValueError("CodeBuddy history changed during import; snapshot staged but not activated")
    if not existing:
        index["conversations"].append(conversation)
    if activate:
        index["current"] = conversation_id
    try:
        _atomic(index_path, index)
        if activate:
            _atomic(history_dir / "current.json", {"conversationId": conversation_id,
                    "lastUpdated": datetime.now(timezone.utc).isoformat()})
    except Exception:
        shutil.copyfile(backup / "index.json", index_path)
        raise
    return {"status": "imported" if not existing else "unchanged", "conversation_id": conversation_id,
            "name": conversation["name"], "messages": len(snapshot["messages"]),
            "requests": len(snapshot["index"]["requests"]), "backup": str(backup),
            "history_dir": str(history_dir), "activated": activate}
