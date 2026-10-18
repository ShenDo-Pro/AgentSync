"""Native history conversion, preservation, and offline import regressions."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agentsync.codebuddy_history import build_snapshot, import_snapshot


class HistoryImportTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.history = self.root / "history"
        self.history.mkdir()
        self.original = {"conversations": [{"id": "native", "type": "craft", "name": "Existing"}],
                         "current": "native"}
        (self.history / "index.json").write_text(json.dumps(self.original), encoding="utf-8")
        (self.history / "native").mkdir()
        (self.history / "native/keep.txt").write_text("Do not change", encoding="utf-8")
        self.rollout = self.root / "rollout-test.jsonl"
        self.records = [{"type": "session_meta", "payload": {"id": "task-1", "cwd": str(self.root)}}]
        self.add_message("user", "<environment_context>hidden setup</environment_context>")
        self.add_message("developer", "Never export internal instructions")
        self.add_message("user", "修复登录")
        self.add_message("assistant", "正在检查", "commentary")
        self.add_message("assistant", "Not visible", "analysis")
        self.add_message("assistant", "登录已修复", "final_answer")
        self.add_message("user", "下一步")
        self.write_rollout()
        running = patch("agentsync.codebuddy_history.codebuddy_running", return_value=False)
        self.running = running.start()
        self.addCleanup(running.stop)

    def add_message(self, role, content, phase=""):
        self.records.append({"type": "response_item", "timestamp": "2026-10-18T15:00:00Z",
                             "payload": {"type": "message", "role": role, "phase": phase,
                                         "content": [{"type": "input_text" if role == "user" else "output_text",
                                                      "text": content}]}})

    def write_rollout(self):
        self.rollout.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in self.records),
                                encoding="utf-8")

    def test_visible_dialogue_and_request_groups(self):
        snapshot = build_snapshot(self.rollout)
        texts = [json.loads(m["message"])["content"][0]["text"] for m in snapshot["messages"].values()]
        self.assertEqual(texts, ["修复登录", "正在检查", "登录已修复", "下一步"])
        self.assertEqual([len(r["messages"]) for r in snapshot["index"]["requests"]], [3, 1])
        self.assertTrue(all(r["state"] == "complete" for r in snapshot["index"]["requests"]))

    def test_import_backup_activation_and_idempotence(self):
        snapshot = build_snapshot(self.rollout)
        result = import_snapshot(snapshot, self.history, self.root / "backups", True)
        index = json.loads((self.history / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(index["current"], result["conversation_id"])
        self.assertEqual(index["conversations"][0], self.original["conversations"][0])
        self.assertEqual(json.loads((Path(result["backup"]) / "index.json").read_text()), self.original)
        self.assertEqual((self.history / "native/keep.txt").read_text(), "Do not change")
        self.assertEqual(json.loads((self.history / "current.json").read_text())["conversationId"], index["current"])
        conversation_path = self.history / result["conversation_id"]
        for m in snapshot["index"]["messages"]:
            actual = json.loads((conversation_path / "messages" / (m["id"] + ".json")).read_text(encoding="utf-8"))
            self.assertEqual(actual, snapshot["messages"][m["id"]])
        repeat = import_snapshot(snapshot, self.history, self.root / "backups")
        self.assertEqual(repeat["status"], "unchanged")
        self.assertEqual(len(list((self.root / "backups").iterdir())), 1)

    def test_new_snapshot_preserves_native_continuation(self):
        first = import_snapshot(build_snapshot(self.rollout), self.history, self.root / "backups")
        continued = self.history / first["conversation_id"] / "index.json"
        continued.write_text('{"messages": [], "requests": [], "nativeContinuation": true}')
        self.add_message("assistant", "第二轮完成", "final_answer")
        self.write_rollout()
        second = import_snapshot(build_snapshot(self.rollout), self.history, self.root / "backups")
        self.assertNotEqual(first["conversation_id"], second["conversation_id"])
        self.assertTrue(json.loads(continued.read_text())["nativeContinuation"])

    def test_running_app_prevents_any_write(self):
        self.running.return_value = True
        with self.assertRaisesRegex(ValueError, "Close CodeBuddyCN"):
            import_snapshot(build_snapshot(self.rollout), self.history, self.root / "backups")
        self.assertFalse((self.root / "backups").exists())
        self.assertEqual(json.loads((self.history / "index.json").read_text()), self.original)

    def test_windows_deep_account_directory_paths_fit(self):
        snapshot = build_snapshot(self.rollout)
        native_base = ("C:/Users/admin/AppData/Local/CodeBuddyExtension/Data/"
                       "76eda867-4c5f-4efc-8644-53d7d0beb66f/CodeBuddyIDE/"
                       "76eda867-4c5f-4efc-8644-53d7d0beb66f/history/"
                       "410de74f6536be04d0613f17fde8b0db")
        for message_id in snapshot["messages"]:
            path = native_base + "/" + snapshot["conversation"]["id"] + "/messages/" + message_id + ".json"
            self.assertLess(len(path), 260)

    def test_concurrent_index_change_is_not_overwritten(self):
        def check_running():
            if self.running.call_count == 2:
                (self.history / "index.json").write_text('{"conversations": [], "current": "new"}')
            return False
        self.running.side_effect = check_running
        with self.assertRaisesRegex(ValueError, "changed during import"):
            import_snapshot(build_snapshot(self.rollout), self.history, self.root / "backups", True)
        self.assertEqual(json.loads((self.history / "index.json").read_text())["current"], "new")


if __name__ == "__main__":
    unittest.main()
