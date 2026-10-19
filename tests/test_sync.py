"""Two-device replication and cross-IDE message bridge integration checks."""

import json
import os
import subprocess
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

import test_handoff
from agentsync.cloud import CloudHandler
from agentsync.daemon import flush, sync
from agentsync.identity import resolve_project
from agentsync.messages import changes, publish
from agentsync.replication import pull
from agentsync.sources import register, scan_registered
from agentsync.storage import (acknowledge_batch, connect, init_cloud, init_local,
                               pending, select_handoff, save_event)


class SyncTest(unittest.TestCase):
    setUp = test_handoff.HandoffTest.setUp

    def server(self):
        with connect(self.cloud_db) as db:
            init_cloud(db)
        server = ThreadingHTTPServer(("127.0.0.1", 0), CloudHandler)
        server.db_path, server.token = self.cloud_db, "token"
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return "http://127.0.0.1:%d" % server.server_port

    def emit(self, source="trae", message_id="m1", content="你好", db_path=None, **kwargs):
        return publish(db_path or self.local_db, str(self.repo), source, "task", message_id,
                       "user", content, **kwargs)

    def test_two_devices_bidirectional_and_no_echo(self):
        url = self.server()
        second = str(self.root / "second.db")
        registration = register(second, str(self.repo))
        self.emit()
        self.assertEqual(self.emit()["status"], "unchanged")
        self.assertGreater(flush(self.local_db, url, "token"), 0)
        self.assertEqual(sync(second, url, "token")["downloaded"], 1)
        self.assertEqual(pull(second, url, "token", registration["project_id"]), 0)
        with connect(second) as db:
            self.assertEqual(pending(db), [])
            self.assertEqual(db.execute("SELECT content FROM events").fetchone()[0], "你好")
        self.emit("codebuddy", "reply", "已完成", second)
        sync(second, url, "token")
        self.assertEqual(sync(self.local_db, url, "token")["downloaded"], 1)
        self.assertEqual(sync(self.local_db, url, "token"), {"uploaded": 0, "downloaded": 0})

    def test_pagination_and_late_message(self):
        project = resolve_project(str(self.repo))
        for i in range(5):
            self.emit(message_id=str(i), content=str(i), timestamp="2026-10-19T00:00:00Z", sequence=i)
        with connect(self.local_db) as db:
            page = changes(db, project.project_id, limit=2)
            self.assertTrue(page["has_more"])
            next_page = changes(db, project.project_id, page["next_cursor"], 2)
            self.assertEqual([e["content"] for e in next_page["events"]], ["2", "3"])
            self.assertEqual(changes(db, "different-project")["events"], [])
        self.emit(message_id="older", content="old", timestamp="2020-01-01T00:00:00Z")
        with connect(self.local_db) as db:
            self.assertIn("4", select_handoff(db, project.project_id, project.branch)["handoff"]["summary"])
            self.assertEqual(changes(db, project.project_id, 5)["events"][0]["content"], "old")

    def test_auth_invalid_cursor_and_atomic_invalid_batch(self):
        url = self.server()
        with self.assertRaises(HTTPError) as caught:
            urlopen(url + "/v1/changes?project_id=x")
        self.assertEqual(caught.exception.code, 401)
        request = Request(url + "/v1/changes?project_id=x&after=-1", headers={"Authorization": "Bearer token"})
        with self.assertRaises(HTTPError) as caught:
            urlopen(request)
        self.assertEqual(caught.exception.code, 400)
        self.emit()
        with connect(self.local_db) as db:
            event = dict(db.execute("SELECT * FROM events").fetchone())
        request = Request(url + "/v1/events", data=json.dumps({"events": [event, {**event, "content": []}]}).encode(),
                          headers={"Authorization": "Bearer token"})
        with self.assertRaises(HTTPError) as caught:
            urlopen(request)
        self.assertEqual(caught.exception.code, 400)
        with connect(self.cloud_db) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM events").fetchone()[0], 0)

    def test_ack_preserves_concurrent_update(self):
        self.emit()
        with connect(self.local_db) as db:
            batch = pending(db)
            row = next(r for r in batch if r["kind"] == "handoff")
            db.execute("UPDATE outbox SET payload=? WHERE queue_id=?", ('{"new":true}', row["queue_id"]))
            acknowledge_batch(db, batch)
            self.assertEqual(len(pending(db)), 1)

    def test_native_new_messages_and_import_loop_prevention(self):
        native = self.root / "native"
        folder = native / "conversation" / "messages"
        folder.mkdir(parents=True)
        (native / "index.json").write_text(json.dumps({"conversations": [{"id": "conversation"}]}))
        entries = []
        for i in range(3):
            entries.append({"id": str(i), "role": "user", "isComplete": i < 2})
            record = {"message": json.dumps({"content": [{"type": "text", "text": str(i)}]}),
                      "createdAt": "2026-10-19T00:00:00Z", "extra": json.dumps({"agentsync": {"source": "codex"}} if i == 0 else {})}
            (folder / (str(i) + ".json")).write_text(json.dumps(record))
        index = folder.parent / "index.json"
        index.write_text(json.dumps({"messages": entries}))
        register(self.local_db, str(self.repo), str(native))
        self.assertEqual(scan_registered(self.local_db), 1)
        self.assertEqual(scan_registered(self.local_db), 0)
        entries[2]["isComplete"] = True
        index.write_text(json.dumps({"messages": entries}))
        self.assertEqual(scan_registered(self.local_db), 1)

    def test_mcp_stdio_publish_read_and_errors(self):
        requests = [
            {"id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
            {"method": "notifications/initialized"},
            {"id": 2, "method": "tools/list"},
            {"id": 3, "method": "tools/call", "params": {"name": "agentsync_publish", "arguments": {
                "path": str(self.repo), "source": "trae", "task_id": "task", "message_id": "m1",
                "role": "user", "content": "跨工具消息"}}},
            {"id": 4, "method": "tools/call", "params": {"name": "agentsync_messages", "arguments": {"path": str(self.repo)}}},
            {"id": 5, "method": "tools/call", "params": {"name": "missing", "arguments": {"path": str(self.repo)}}},
        ]
        payload = "\n".join(json.dumps({"jsonrpc": "2.0", **r}) for r in requests) + "\n"
        result = subprocess.run([sys.executable, "-m", "agentsync.cli", "mcp"], input=payload.encode(),
                                capture_output=True, check=True, env={**os.environ, "AGENTSYNC_DB": self.local_db})
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(replies), 5)
        self.assertEqual(len(replies[1]["result"]["tools"]), 3)
        self.assertIn("跨工具消息", replies[3]["result"]["content"][0]["text"])
        self.assertTrue(replies[4]["result"]["isError"])

    def test_invalid_page_does_not_advance_cursor(self):
        project = resolve_project(str(self.repo))
        self.emit()
        with connect(self.local_db) as db:
            event = dict(db.execute("SELECT * FROM events").fetchone())
        second = str(self.root / "second.db")
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return json.dumps({"events": [event, {**event, "project_id": "wrong"}], "next_cursor": 2}).encode()
        with patch("agentsync.replication.urlopen", return_value=Response()):
            with self.assertRaises(ValueError):
                pull(second, "http://cloud", "token", project.project_id)
        with connect(second) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM events").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM sync_cursors").fetchone()[0], 0)

    def test_cloud_multiple_pages_resume_after_network_failure(self):
        url = self.server()
        self.emit()
        with connect(self.local_db) as db:
            original = dict(db.execute("SELECT * FROM events").fetchone())
            for i in range(204):
                save_event(db, {**original, "event_id": "message-" + str(i), "content": str(i), "source_offset": i + 1})
        flush(self.local_db, url, "token")
        second = str(self.root / "second.db")
        from agentsync import replication
        actual_open = replication.urlopen
        calls = 0
        def interrupted(request, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("connection interrupted")
            return actual_open(request, **kwargs)
        with patch("agentsync.replication.urlopen", side_effect=interrupted):
            with self.assertRaises(OSError):
                pull(second, url, "token", original["project_id"])
        with connect(second) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM events").fetchone()[0], 100)
            self.assertEqual(db.execute("SELECT cursor FROM sync_cursors").fetchone()[0], 100)
        self.assertEqual(pull(second, url, "token", original["project_id"]), 105)
        with connect(second) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM events").fetchone()[0], 205)
            self.assertEqual(pending(db), [])

    def test_cloud_missing_does_not_hide_local_handoff(self):
        from agentsync.codebuddy import handoff
        url = self.server()
        self.emit()
        self.assertEqual(handoff(str(self.repo), self.local_db, url, "token")["status"], "ready")
