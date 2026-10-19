"""Restart-safe cloud downloads. Downloaded events never enter the upload queue."""

import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .messages import validate_event
from .storage import connect, init_local, refresh_handoff, save_event


def pull(db_path: str, cloud_url: str, token: str, project_id: str) -> int:
    if not cloud_url or not token:
        return 0
    endpoint = cloud_url.rstrip("/")
    count = 0
    with connect(db_path) as db:
        init_local(db)
        db.execute("CREATE TABLE IF NOT EXISTS sync_cursors (endpoint TEXT, project_id TEXT, "
                   "cursor INTEGER NOT NULL, PRIMARY KEY(endpoint, project_id))")
        while True:
            row = db.execute("SELECT cursor FROM sync_cursors WHERE endpoint=? AND project_id=?",
                             (endpoint, project_id)).fetchone()
            cursor = row[0] if row else 0
            request = Request(endpoint + "/v1/changes?" + urlencode({"project_id": project_id, "after": cursor}),
                              headers={"Authorization": "Bearer " + token})
            with urlopen(request, timeout=10) as response:
                page = json.load(response)
            events, next_cursor = page["events"], page["next_cursor"]
            if not isinstance(events, list) or type(next_cursor) is not int or next_cursor < cursor:
                raise ValueError("invalid cloud cursor")
            if (events or page.get("has_more")) and next_cursor <= cursor:
                raise ValueError("cloud cursor did not advance")
            tasks = {}
            for event in events:
                validate_event(event)
                if event["project_id"] != project_id:
                    raise ValueError("cloud returned a different project")
                if save_event(db, event, enqueue=False):
                    count += 1
                    tasks[event["task_id"]] = event["branch"]
            for task, branch in tasks.items():
                # Derived local handoffs must not bounce back to their source.
                refresh_handoff(db, task, project_id, branch, "", enqueue=False)
            db.execute("INSERT INTO sync_cursors VALUES (?, ?, ?) ON CONFLICT(endpoint, project_id) "
                       "DO UPDATE SET cursor=excluded.cursor", (endpoint, project_id, next_cursor))
            db.commit()
            if not page.get("has_more"):
                break
    return count
