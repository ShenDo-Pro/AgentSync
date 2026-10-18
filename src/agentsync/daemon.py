"""Collect Codex dialogue and retry cloud uploads from a local queue."""

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .codex import dialogue_event, new_lines, rollout_files, session_metadata
from .identity import resolve_project
from .storage import (acknowledge, connect, file_state, init_local, pending,
                      refresh_handoff, save_event, save_file_state,
                      save_project_state)

LOG = logging.getLogger(__name__)


def scan(db_path: str, codex_home: Path) -> int:
    """Collect complete new dialogue records from Codex rollouts."""
    count = 0
    with connect(db_path) as db:
        init_local(db)
        roots = set()
        for path in rollout_files(codex_home):
            try:
                key = str(path.resolve())
                state = file_state(db, key)
                if state:
                    task_id = state["task_id"]
                    project_id = state["project_id"]
                    remote = state["remote"]
                    root = state["root"]
                    branch = state["branch"]
                    head = state["head"]
                    offset = state["offset"]
                else:
                    metadata = session_metadata(path)
                    if not metadata or not metadata.get("id"):
                        continue
                    project = resolve_project(metadata.get("cwd", ""))
                    if not project:
                        continue
                    task_id = metadata["id"]
                    project_id = project.project_id
                    remote = project.remote
                    root = project.root
                    recorded_git = metadata.get("git") or {}
                    branch = recorded_git.get("branch") or project.branch
                    head = recorded_git.get("commit_hash") or project.head
                    offset = 0
                if path.stat().st_size < offset:
                    offset = 0
                records, next_offset = new_lines(path, offset)
                changed = False
                for source_offset, item in records:
                    event = dialogue_event(item, task_id, source_offset)
                    if event:
                        if not event["source_timestamp"]:
                            event["source_timestamp"] = datetime.fromtimestamp(
                                path.stat().st_mtime, timezone.utc).isoformat()
                        event["project_id"] = project_id
                        event["branch"] = branch
                        inserted = save_event(db, event)
                        changed |= inserted
                        count += int(inserted)
                if changed:
                    refresh_handoff(db, task_id, project_id, branch, head)
                save_file_state(db, key, task_id, project_id, remote,
                                root, branch, head, next_offset)
                roots.add(root)
                db.commit()
            except (OSError, ValueError) as error:
                LOG.warning("Cannot scan %s: %s", path, error)
        for root in roots:
            project = resolve_project(root)
            if project:
                save_project_state(db, project)
        db.commit()
    return count


def _post(url: str, token: str, payload: dict) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(url, data=data, method="POST", headers={
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
    })
    with urlopen(request, timeout=10) as response:
        if response.status != 200:
            raise OSError("unexpected server status %s" % response.status)


def flush(db_path: str, cloud_url: str, token: str) -> int:
    """Upload pending records and acknowledge only successful requests."""
    if not cloud_url or not token:
        return 0
    sent = 0
    with connect(db_path) as db:
        init_local(db)
        while True:
            batch = pending(db, 100)
            if not batch:
                break
            first_kind = batch[0]["kind"]
            same_kind = [row for row in batch if row["kind"] == first_kind]
            try:
                if first_kind == "event":
                    _post(cloud_url.rstrip("/") + "/v1/events", token,
                          {"events": [json.loads(row["payload"]) for row in same_kind]})
                elif first_kind == "handoff":
                    for row in same_kind:
                        _post(cloud_url.rstrip("/") + "/v1/handoffs", token,
                              {"handoff": json.loads(row["payload"])})
                        acknowledge(db, [row["queue_id"]])
                        sent += 1
                    continue
                elif first_kind == "state":
                    for row in same_kind:
                        _post(cloud_url.rstrip("/") + "/v1/project-states", token,
                              {"state": json.loads(row["payload"])})
                        acknowledge(db, [row["queue_id"]])
                        sent += 1
                    continue
                else:
                    raise ValueError("unknown outbox kind")
            except (OSError, HTTPError, URLError, ValueError) as error:
                LOG.warning("Cloud upload postponed: %s", error)
                break
            acknowledge(db, [row["queue_id"] for row in same_kind])
            sent += len(same_kind)
    return sent


def run(db_path: str, codex_home: Path, cloud_url: str,
        token: str, interval: float = 5.0) -> None:
    """Continuously scan Codex sessions and retry offline uploads."""
    while True:
        try:
            collected = scan(db_path, codex_home)
            uploaded = flush(db_path, cloud_url, token)
            if collected or uploaded:
                LOG.info("Collected %d records; uploaded %d items", collected, uploaded)
        except (OSError, ValueError) as error:
            LOG.warning("Daemon cycle postponed: %s", error)
        time.sleep(max(interval, 1.0))


def main() -> None:
    """Run the daemon from environment configuration."""
    logging.basicConfig(level=logging.INFO)
    run(os.environ.get("AGENTSYNC_DB", "~/.agentsync/local.db"),
        Path(os.environ.get("AGENTSYNC_CODEX_HOME", "~/.codex")).expanduser(),
        os.environ.get("AGENTSYNC_CLOUD_URL", ""),
        os.environ.get("AGENTSYNC_TOKEN", ""),
        float(os.environ.get("AGENTSYNC_INTERVAL", "5")))
