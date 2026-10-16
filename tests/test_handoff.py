"""End-to-end checks for offline collection and Codex task handoff."""

import json
import io
import os
import subprocess
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from agentsync.cloud import CloudHandler
from agentsync.daemon import flush, scan
from agentsync.codebuddy import handoff, hook_main, install_hook
from agentsync.identity import normalize_remote, resolve_project
from agentsync.storage import connect, init_cloud, init_local, pending


def git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True,
                   capture_output=True, text=True)


class HandoffTest(unittest.TestCase):
    """Verify one task transfers while other tasks stay separate."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init")
        git(self.repo, "config", "user.email", "test@example.com")
        git(self.repo, "config", "user.name", "AgentSync Test")
        (self.repo / "README.md").write_text("sample\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "initial")
        git(self.repo, "remote", "add", "origin", "git@github.com:example/project.git")
        self.codex_home = self.root / "codex"
        self.sessions = self.codex_home / "sessions" / "2026" / "10" / "16"
        self.sessions.mkdir(parents=True)
        self.local_db = str(self.root / "local.db")
        self.cloud_db = str(self.root / "cloud.db")

    def write_session(self, task_id: str, user_text: str, answer: str) -> None:
        timestamp = "2026-10-16T00:00:01Z" if task_id == "task-a" else "2026-10-16T00:00:02Z"
        records = [
            {"type": "session_meta", "payload": {"id": task_id, "cwd": str(self.repo)}},
            {"timestamp": timestamp, "type": "response_item", "payload": {"type": "message", "role": "user",
                "content": [{"type": "input_text", "text": user_text}]}},
            {"timestamp": timestamp, "type": "response_item", "payload": {"type": "message", "role": "assistant",
                "phase": "final_answer", "content": [{"type": "output_text", "text": answer}]}}
        ]
        path = self.sessions / ("rollout-2026-10-16T00-00-00-" + task_id + ".jsonl")
        with path.open("w", encoding="utf-8") as stream:
            for record in records:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")

    def test_offline_queue_sync_and_ambiguity(self) -> None:
        self.write_session("task-a", "Fix login", "Login fixed")
        self.assertEqual(scan(self.local_db, self.codex_home), 2)
        self.assertEqual(scan(self.local_db, self.codex_home), 0)
        local = handoff(str(self.repo), self.local_db)
        self.assertEqual(local["status"], "ready")
        self.assertIn("Fix login", local["handoff"]["summary"])
        self.assertIn("Login fixed", local["handoff"]["summary"])
        self.assertEqual(flush(self.local_db, "http://127.0.0.1:1", "token"), 0)
        with connect(self.local_db) as db:
            init_local(db)
            self.assertEqual(len(pending(db)), 4)

        with connect(self.cloud_db) as db:
            init_cloud(db)
        server = ThreadingHTTPServer(("127.0.0.1", 0), CloudHandler)
        server.db_path = self.cloud_db
        server.token = "token"
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = "http://127.0.0.1:%d" % server.server_port
        self.assertEqual(flush(self.local_db, url, "token"), 4)
        remote = handoff(str(self.repo), self.local_db, url, "token")
        self.assertEqual(remote["status"], "ready")
        self.assertEqual(remote["handoff"]["task_id"], "task-a")

        self.write_session("task-b", "Fix billing", "Billing fixed")
        self.assertEqual(scan(self.local_db, self.codex_home), 2)
        self.assertEqual(flush(self.local_db, url, "token"), 3)
        latest = handoff(str(self.repo), self.local_db, url, "token")
        self.assertEqual(latest["status"], "ready")
        self.assertEqual(latest["handoff"]["task_id"], "task-b")
        selected = handoff(str(self.repo), self.local_db, url, "token", "task-b")
        self.assertEqual(selected["handoff"]["task_id"], "task-b")
        self.assertNotIn("Fix login", selected["handoff"]["summary"])

    def test_remote_identity_matches_transport_forms(self) -> None:
        self.assertEqual(normalize_remote("git@github.com:example/project.git"),
                         normalize_remote("https://github.com/example/project.git"))
        self.assertEqual(resolve_project(str(self.repo)).remote,
                         "github.com/example/project")

    def test_codebuddy_hook_injects_context_once(self) -> None:
        self.write_session("task-a", "Fix login", "Login fixed")
        scan(self.local_db, self.codex_home)
        output = io.StringIO()
        with patch.dict(os.environ, {"AGENTSYNC_DB": self.local_db,
                                      "AGENTSYNC_CLOUD_URL": "",
                                      "AGENTSYNC_TOKEN": ""}), \
             patch("sys.stdin", io.StringIO(json.dumps({"cwd": str(self.repo)}))), \
             redirect_stdout(output):
            hook_main()
        result = json.loads(output.getvalue())
        context = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Fix login", context)
        self.assertIn("task-a", context)

        settings_path = self.root / "settings.json"
        settings_path.write_text('{"language":"zh-CN"}', encoding="utf-8")
        install_hook(settings_path)
        install_hook(settings_path)
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertEqual(settings["language"], "zh-CN")
        self.assertEqual(len(settings["hooks"]["SessionStart"]), 1)


if __name__ == "__main__":
    unittest.main()
