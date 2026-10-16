"""SQLite storage for local sessions, pending uploads, and cloud history."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def connect(path: str) -> sqlite3.Connection:
    """Open a durable SQLite connection."""
    database = Path(path).expanduser()
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(database), timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=15000")
    return connection


def init_local(db: sqlite3.Connection) -> None:
    """Create the local session and upload queue schema."""
    db.executescript("""
        CREATE TABLE IF NOT EXISTS files (
            path TEXT PRIMARY KEY, task_id TEXT NOT NULL, project_id TEXT NOT NULL,
            remote TEXT NOT NULL, root TEXT NOT NULL, branch TEXT NOT NULL, head TEXT NOT NULL,
            offset INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS events (
            event_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
            task_id TEXT NOT NULL, branch TEXT NOT NULL,
            role TEXT NOT NULL, content TEXT NOT NULL, raw_record TEXT NOT NULL,
            phase TEXT NOT NULL,
            source_offset INTEGER NOT NULL, source_timestamp TEXT NOT NULL,
            ingested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS events_task ON events(task_id, ingested_at);
        CREATE TABLE IF NOT EXISTS handoffs (
            task_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
            branch TEXT NOT NULL, head TEXT NOT NULL,
            summary TEXT NOT NULL, source_event_ids TEXT NOT NULL,
            version INTEGER NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS handoffs_project ON handoffs(project_id, branch, updated_at);
        CREATE TABLE IF NOT EXISTS project_states (
            project_id TEXT NOT NULL, branch TEXT NOT NULL, head TEXT NOT NULL,
            tracked_changes TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY(project_id, branch)
        );
        CREATE TABLE IF NOT EXISTS outbox (
            queue_id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL, item_key TEXT NOT NULL,
            payload TEXT NOT NULL, UNIQUE(kind, item_key)
        );
    """)
    db.commit()


def init_cloud(db: sqlite3.Connection) -> None:
    """Create the cloud event and handoff schema."""
    db.executescript("""
        CREATE TABLE IF NOT EXISTS events (
            event_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
            task_id TEXT NOT NULL, branch TEXT NOT NULL,
            role TEXT NOT NULL, content TEXT NOT NULL, raw_record TEXT NOT NULL,
            phase TEXT NOT NULL,
            source_offset INTEGER NOT NULL, source_timestamp TEXT NOT NULL,
            received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS cloud_events_project ON events(project_id, branch, task_id);
        CREATE TABLE IF NOT EXISTS handoffs (
            task_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
            branch TEXT NOT NULL, head TEXT NOT NULL,
            summary TEXT NOT NULL, source_event_ids TEXT NOT NULL,
            version INTEGER NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS cloud_handoffs_project ON handoffs(project_id, branch, updated_at);
        CREATE TABLE IF NOT EXISTS project_states (
            project_id TEXT NOT NULL, branch TEXT NOT NULL, head TEXT NOT NULL,
            tracked_changes TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY(project_id, branch)
        );
    """)
    db.commit()


def file_state(db: sqlite3.Connection, path: str) -> sqlite3.Row | None:
    """Read the persisted offset and identity for a rollout file."""
    return db.execute("SELECT * FROM files WHERE path=?", (path,)).fetchone()


def save_file_state(db: sqlite3.Connection, path: str, task_id: str,
                    project_id: str, remote: str, root: str, branch: str,
                    head: str, offset: int) -> None:
    """Persist the rollout cursor and its original project identity."""
    db.execute("""
        INSERT INTO files(path, task_id, project_id, remote, root, branch, head, offset)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET offset=excluded.offset
    """, (path, task_id, project_id, remote, root, branch, head, offset))


def save_project_state(db: sqlite3.Connection, project: object) -> bool:
    """Queue a project snapshot when tracked Git state changes."""
    changes = json.dumps(project.tracked_changes, ensure_ascii=False)
    previous = db.execute("""
        SELECT head, tracked_changes FROM project_states
        WHERE project_id=? AND branch=?
    """, (project.project_id, project.branch)).fetchone()
    if previous and previous["head"] == project.head and previous["tracked_changes"] == changes:
        return False
    state = dict(project_id=project.project_id, branch=project.branch,
                 head=project.head, tracked_changes=list(project.tracked_changes),
                 updated_at=datetime.now(timezone.utc).isoformat())
    db.execute("""
        INSERT INTO project_states(project_id, branch, head, tracked_changes, updated_at)
        VALUES (:project_id, :branch, :head, :encoded_changes, :updated_at)
        ON CONFLICT(project_id, branch) DO UPDATE SET
            head=excluded.head, tracked_changes=excluded.tracked_changes,
            updated_at=excluded.updated_at
    """, {**state, "encoded_changes": changes})
    key = project.project_id + ":" + project.branch
    db.execute("""
        INSERT INTO outbox(kind, item_key, payload) VALUES ('state', ?, ?)
        ON CONFLICT(kind, item_key) DO UPDATE SET payload=excluded.payload
    """, (key, json.dumps(state, ensure_ascii=False)))
    return True


def save_event(db: sqlite3.Connection, event: dict) -> bool:
    """Store a dialogue event and queue its cloud upload once."""
    cursor = db.execute("""
        INSERT OR IGNORE INTO events
        (event_id, project_id, task_id, branch, role, content, raw_record, phase,
         source_offset, source_timestamp)
        VALUES (:event_id, :project_id, :task_id, :branch, :role, :content, :raw_record,
                :phase, :source_offset, :source_timestamp)
    """, event)
    if cursor.rowcount:
        db.execute("""
            INSERT OR IGNORE INTO outbox(kind, item_key, payload)
            VALUES ('event', ?, ?)
        """, (event["event_id"], json.dumps(event, ensure_ascii=False)))
    return bool(cursor.rowcount)


def refresh_handoff(db: sqlite3.Connection, task_id: str, project_id: str,
                    branch: str, head: str) -> dict | None:
    """Build a source-linked summary from the latest dialogue."""
    rows = db.execute("""
        SELECT rowid, event_id, role, content, source_timestamp FROM events
        WHERE task_id=? ORDER BY rowid DESC LIMIT 100
    """, (task_id,)).fetchall()
    user = next((row for row in rows if row["role"] == "user"), None)
    if not user:
        return None
    assistant = next((row for row in rows if row["role"] == "assistant"
                      and row["rowid"] > user["rowid"]), None)
    source_ids = [user["event_id"]]
    summary = "Codex 最近任务：\n" + user["content"][-4000:]
    if assistant:
        source_ids.append(assistant["event_id"])
        summary += "\n\nCodex 最近答复：\n" + assistant["content"][-4000:]
    if head:
        summary += "\n\n记录时的 Git HEAD：" + head
    previous = db.execute("SELECT version, source_event_ids FROM handoffs WHERE task_id=?",
                          (task_id,)).fetchone()
    encoded_ids = json.dumps(source_ids)
    if previous and previous["source_event_ids"] == encoded_ids:
        return None
    version = (previous["version"] + 1) if previous else 1
    latest_timestamp = rows[0]["source_timestamp"]
    try:
        updated_at = datetime.fromisoformat(
            latest_timestamp.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
    except ValueError:
        updated_at = datetime.now(timezone.utc).isoformat()
    handoff = dict(task_id=task_id, project_id=project_id, branch=branch,
                   head=head, summary=summary, source_event_ids=source_ids,
                   version=version, updated_at=updated_at)
    db.execute("""
        INSERT INTO handoffs(task_id, project_id, branch, head, summary,
                             source_event_ids, version, updated_at)
        VALUES (:task_id, :project_id, :branch, :head, :summary,
                :encoded_ids, :version, :updated_at)
        ON CONFLICT(task_id) DO UPDATE SET
            summary=excluded.summary, source_event_ids=excluded.source_event_ids,
            version=excluded.version, updated_at=excluded.updated_at
    """, {**handoff, "encoded_ids": encoded_ids})
    db.execute("""
        INSERT INTO outbox(kind, item_key, payload) VALUES ('handoff', ?, ?)
        ON CONFLICT(kind, item_key) DO UPDATE SET payload=excluded.payload
    """, (task_id, json.dumps(handoff, ensure_ascii=False)))
    return handoff


def pending(db: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    """Return queued uploads in insertion order."""
    return db.execute("SELECT * FROM outbox ORDER BY queue_id LIMIT ?", (limit,)).fetchall()


def acknowledge(db: sqlite3.Connection, queue_ids: list[int]) -> None:
    """Remove cloud-acknowledged queue entries."""
    if queue_ids:
        db.executemany("DELETE FROM outbox WHERE queue_id=?", [(i,) for i in queue_ids])
        db.commit()


def cloud_save_events(db: sqlite3.Connection, events: list[dict]) -> None:
    """Insert idempotent dialogue events received from a daemon."""
    for event in events:
        db.execute("""
            INSERT OR IGNORE INTO events
            (event_id, project_id, task_id, branch, role, content, raw_record, phase,
             source_offset, source_timestamp)
            VALUES (:event_id, :project_id, :task_id, :branch, :role,
                    :content, :raw_record, :phase, :source_offset, :source_timestamp)
        """, event)
    db.commit()


def cloud_save_handoff(db: sqlite3.Connection, handoff: dict) -> None:
    """Keep the newest version of a task handoff."""
    db.execute("""
        INSERT INTO handoffs(task_id, project_id, branch, head, summary,
                             source_event_ids, version, updated_at)
        VALUES (:task_id, :project_id, :branch, :head, :summary,
                :encoded_ids, :version, :updated_at)
        ON CONFLICT(task_id) DO UPDATE SET
            summary=excluded.summary, source_event_ids=excluded.source_event_ids,
            version=excluded.version, updated_at=excluded.updated_at
        WHERE excluded.version > handoffs.version
    """, {**handoff, "encoded_ids": json.dumps(handoff["source_event_ids"])})
    db.commit()


def cloud_save_project_state(db: sqlite3.Connection, state: dict) -> None:
    """Keep the newest snapshot of a project's tracked Git state."""
    db.execute("""
        INSERT INTO project_states(project_id, branch, head, tracked_changes, updated_at)
        VALUES (:project_id, :branch, :head, :encoded_changes, :updated_at)
        ON CONFLICT(project_id, branch) DO UPDATE SET
            head=excluded.head, tracked_changes=excluded.tracked_changes,
            updated_at=excluded.updated_at
        WHERE excluded.updated_at > project_states.updated_at
    """, {**state, "encoded_changes": json.dumps(state["tracked_changes"], ensure_ascii=False)})
    db.commit()


def project_states(db: sqlite3.Connection, project_id: str) -> list[dict]:
    """Return current branches and tracked changes for a project."""
    rows = db.execute("SELECT * FROM project_states WHERE project_id=?",
                      (project_id,)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["tracked_changes"] = json.loads(item["tracked_changes"])
        result.append(item)
    return result


def select_handoff(db: sqlite3.Connection, project_id: str, branch: str,
                   task_id: str = "") -> dict:
    """Select the latest distinct task or an explicitly chosen task."""
    if task_id:
        rows = db.execute("""
            SELECT * FROM handoffs WHERE project_id=? AND branch=? AND task_id=?
        """, (project_id, branch, task_id)).fetchall()
    else:
        rows = db.execute("""
            SELECT * FROM handoffs WHERE project_id=? AND branch=?
            ORDER BY updated_at DESC LIMIT 2
        """, (project_id, branch)).fetchall()
    if not rows:
        return {"status": "missing"}
    if len(rows) > 1 and rows[0]["updated_at"] == rows[1]["updated_at"]:
        return {"status": "ambiguous", "task_ids": [row["task_id"] for row in rows]}
    handoff = dict(rows[0])
    handoff["source_event_ids"] = json.loads(handoff["source_event_ids"])
    return {"status": "ready", "handoff": handoff}


def history(db: sqlite3.Connection, project_id: str, limit: int = 100) -> list[dict]:
    """Return read-only project dialogue for a future CCHV adapter."""
    rows = db.execute("""
        SELECT event_id, task_id, branch, role, content, raw_record, phase,
               source_timestamp FROM events WHERE project_id=?
        ORDER BY rowid DESC LIMIT ?
    """, (project_id, max(1, min(limit, 500)))).fetchall()
    return [dict(row) for row in rows]
