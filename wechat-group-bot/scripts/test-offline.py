"""Windows/Linux 共用的离线测试入口；不读真实配置、不联网、不发送消息。"""
from __future__ import annotations

from pathlib import Path
import runpy
import sys
import tempfile
import unittest


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    tests = root / "tests"
    suite = unittest.defaultTestLoader.discover(str(tests), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        return 1

    # 原有的两个算法脚本使用 assert，不属于 unittest 测试数量。
    # 合成临时文件放在项目内，避免 Windows 临时目录的权限差异。
    temp_root = root / ".test-tmp"
    temp_root.mkdir(exist_ok=True)
    previous_tempdir = tempfile.tempdir
    try:
        with tempfile.TemporaryDirectory(prefix="offline-", dir=temp_root) as temp_dir:
            tempfile.tempdir = temp_dir
            for filename in ("test_parsing.py", "test_crypto.py"):
                runpy.run_path(str(tests / filename), run_name="__main__")
    finally:
        tempfile.tempdir = previous_tempdir
    print(f"OK: {result.testsRun} unit tests + parsing and crypto scripts; offline only.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
