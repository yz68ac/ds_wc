"""凭据文件锚定项目根目录；不读取用户真实 .env 或调用 API。"""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from wxbridge.config import deepseek_api_key, load_config


class ProjectEnvTests(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parents[1] / ".test-tmp"
        parent.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.other = self.root / "external-config"
        self.other.mkdir()
        self.config_file = self.other / "config.json"
        self.config_file.write_text("{}", encoding="utf-8")

    def test_external_config_uses_project_env_with_utf8_bom(self):
        (self.root / ".env").write_text("DEEPSEEK_API_KEY=synthetic-project-key\n", encoding="utf-8-sig")
        (self.other / ".env").write_text("DEEPSEEK_API_KEY=synthetic-wrong-key\n", encoding="utf-8")
        with patch("wxbridge.config.PROJECT_ROOT", self.root), patch.dict(os.environ, {}, clear=True):
            cfg = load_config(self.config_file)
            self.assertEqual(deepseek_api_key(cfg), "synthetic-project-key")

    def test_existing_process_environment_keeps_priority(self):
        (self.root / ".env").write_text("DEEPSEEK_API_KEY=synthetic-file-key\n", encoding="utf-8")
        with patch("wxbridge.config.PROJECT_ROOT", self.root), patch.dict(os.environ, {"DEEPSEEK_API_KEY": "synthetic-process-key"}, clear=True):
            cfg = load_config(self.config_file)
            self.assertEqual(deepseek_api_key(cfg), "synthetic-process-key")


if __name__ == "__main__":
    unittest.main()
