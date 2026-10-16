"""Authenticated HTTP server for shared dialogue and handoffs."""

import hmac
import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .storage import (cloud_save_events, cloud_save_handoff, connect, history,
                      cloud_save_project_state, init_cloud, project_states,
                      select_handoff)

LOG = logging.getLogger(__name__)
MAX_BODY = 32 * 1024 * 1024


class CloudHandler(BaseHTTPRequestHandler):
    """Serve authenticated AgentSync JSON endpoints."""

    server_version = "AgentSync/0.1"

    def _reply(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        expected = self.server.token
        actual = self.headers.get("Authorization", "")
        return bool(expected) and hmac.compare_digest(actual, "Bearer " + expected)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length < 1 or length > MAX_BODY:
            raise ValueError("invalid body size")
        data = json.loads(self.rfile.read(length))
        if not isinstance(data, dict):
            raise ValueError("expected JSON object")
        return data

    def do_GET(self) -> None:
        route = urlsplit(self.path)
        if route.path == "/health":
            self._reply(200, {"status": "ok"})
            return
        if not self._authorized():
            self._reply(401, {"error": "unauthorized"})
            return
        query = parse_qs(route.query)
        project_id = query.get("project_id", [""])[0]
        if not project_id:
            self._reply(400, {"error": "project_id required"})
            return
        with connect(self.server.db_path) as db:
            if route.path == "/v1/handoff":
                branch = query.get("branch", [""])[0]
                task_id = query.get("task_id", [""])[0]
                self._reply(200, select_handoff(db, project_id, branch, task_id))
            elif route.path == "/v1/history":
                try:
                    limit = int(query.get("limit", ["100"])[0])
                except ValueError:
                    self._reply(400, {"error": "invalid limit"})
                    return
                self._reply(200, {"events": history(db, project_id, limit)})
            elif route.path == "/v1/project-states":
                self._reply(200, {"states": project_states(db, project_id)})
            else:
                self._reply(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self._authorized():
            self._reply(401, {"error": "unauthorized"})
            return
        try:
            body = self._body()
            with connect(self.server.db_path) as db:
                if self.path == "/v1/events":
                    events = body.get("events")
                    if not isinstance(events, list) or len(events) > 100:
                        raise ValueError("expected at most 100 events")
                    for event in events:
                        if not isinstance(event, dict) or event.get("role") not in ("user", "assistant"):
                            raise ValueError("invalid event")
                        if not all(key in event for key in (
                                "event_id", "project_id", "task_id", "branch",
                                "content", "raw_record", "phase", "source_offset",
                                "source_timestamp")):
                            raise ValueError("incomplete event")
                    cloud_save_events(db, events)
                    self._reply(200, {"accepted": len(events)})
                elif self.path == "/v1/handoffs":
                    handoff = body.get("handoff")
                    if not isinstance(handoff, dict) or not handoff.get("task_id"):
                        raise ValueError("invalid handoff")
                    if not all(key in handoff for key in (
                            "project_id", "branch", "head", "summary",
                            "source_event_ids", "version", "updated_at")):
                        raise ValueError("incomplete handoff")
                    cloud_save_handoff(db, handoff)
                    self._reply(200, {"accepted": True})
                elif self.path == "/v1/project-states":
                    state = body.get("state")
                    if not isinstance(state, dict) or not all(key in state for key in (
                            "project_id", "branch", "head", "tracked_changes", "updated_at")):
                        raise ValueError("invalid project state")
                    cloud_save_project_state(db, state)
                    self._reply(200, {"accepted": True})
                else:
                    self._reply(404, {"error": "not found"})
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            self._reply(400, {"error": str(error)})

    def log_message(self, format: str, *args: object) -> None:
        LOG.info("%s %s", self.address_string(), format % args)


def serve(db_path: str, token: str, bind: str = "127.0.0.1", port: int = 8765) -> None:
    """Run the cloud service with SQLite persistence."""
    if not token or token == "replace-with-a-long-random-token":
        raise ValueError("Set a non-default AGENTSYNC_TOKEN")
    with connect(db_path) as db:
        init_cloud(db)
    server = ThreadingHTTPServer((bind, port), CloudHandler)
    server.db_path = db_path
    server.token = token
    LOG.info("AgentSync Cloud listening on %s:%d", bind, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def main() -> None:
    """Run the cloud service from environment configuration."""
    logging.basicConfig(level=logging.INFO)
    serve(os.environ.get("AGENTSYNC_DB", "~/.agentsync/cloud.db"),
          os.environ.get("AGENTSYNC_TOKEN", ""),
          os.environ.get("AGENTSYNC_BIND", "127.0.0.1"),
          int(os.environ.get("AGENTSYNC_PORT", "8765")))
