"""使用合成数据的离线测试，不联网、不发送微信消息。"""
from __future__ import annotations

import unittest
from unittest.mock import patch
from wxbridge.storage.memoryfs import is_noswap_tmpfs


class MemoryFilesystemTests(unittest.TestCase):
    def test_requires_tmpfs_and_noswap_in_deepest_mount(self):
        # PurePosixPath 使 Windows 也能使用 Linux 路径测试。
        from pathlib import PurePosixPath
        mounts = ("1 0 8:1 / / rw - ext4 /dev/root rw\n"
                  "2 1 0:1 / /run/private rw - tmpfs tmpfs rw,noswap\n"
                  "3 2 8:2 / /run/private/disk rw - ext4 /dev/data rw\n"
                  "4 1 0:2 / /run/swappable rw - tmpfs tmpfs rw\n")
        with patch("wxbridge.storage.memoryfs.Path", PurePosixPath):
            self.assertTrue(is_noswap_tmpfs(PurePosixPath('/run/private/decrypted'), mounts))
            for path in ('/disk', '/run/private/disk/copy', '/run/swappable/copy', '/run/private-other'):
                self.assertFalse(is_noswap_tmpfs(PurePosixPath(path), mounts))


if __name__ == "__main__":
    unittest.main()
