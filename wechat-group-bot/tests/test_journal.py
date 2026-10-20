"""队列只读状态、部署权限校验和 Linux 单实例锁。"""
from __future__ import annotations

from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

from wxbridge.storage.journal import Journal, checked_path, read_status
from wxbridge.app.runtime import instance_lock


class JournalSafetyTests(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parents[1] / ".test-tmp"
        parent.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "queue.sqlite3"

    def test_production_rejects_disk_directory(self):
        # Windows 直接拒绝；Linux /tmp 或工作区也不是 noswap tmpfs。
        with self.assertRaises(ValueError):
            checked_path(str(self.path))

    def test_queue_status_is_read_only_and_does_not_expose_payload(self):
        with patch("wxbridge.storage.journal.checked_path", side_effect=lambda value, **kw: Path(value)):
            journal = Journal(str(self.path), {"account": "self", "chat": "test"}, 19)
            with journal.db:
                journal.set("secret", "sk-NOT-A-REAL-KEY")
            before = self.path.read_bytes()
            status = read_status(str(self.path))
            self.assertEqual(status["cursor"], 19)
            self.assertNotIn("secret", str(status))
            self.assertEqual(self.path.read_bytes(), before)
            journal.close()

    @unittest.skipUnless(sys.platform == "linux", "实际 fcntl 锁在 Linux 验证")
    def test_single_instance_lock_excludes_second_listener(self):
        with patch("wxbridge.app.runtime.checked_path", side_effect=lambda value: Path(value)):
            with instance_lock(str(self.path)):
                with self.assertRaisesRegex(ValueError, "已有监听"):
                    with instance_lock(str(self.path)):
                        self.fail("second listener entered")
            with instance_lock(str(self.path)):
                self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
