"""凭据文件锚定项目根目录；不读取用户真实 .env 或调用 API。"""
from __future__ import annotations

import os
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from wxbridge.config import DEFAULTS, _load_defaults, deepseek_api_key, load_config


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

    def test_json_override_and_environment_keep_precedence_without_mutating_defaults(self):
        before = copy.deepcopy(DEFAULTS)
        self.config_file.write_text(json.dumps({"deepseek": {"temperature": 0.7}, "target": {"chat": "配置中的群", "cooldown_seconds": 3}}), encoding="utf-8")
        with patch("wxbridge.config.PROJECT_ROOT", self.root), patch.dict(os.environ, {"WX_GROUP": "环境中的群", "DEEPSEEK_MODEL": "synthetic-model"}, clear=True):
            cfg = load_config(self.config_file)
        self.assertEqual(cfg["target"]["chat"], "环境中的群")
        self.assertEqual(cfg["target"]["cooldown_seconds"], 3)
        self.assertEqual(cfg["deepseek"]["model"], "synthetic-model")
        self.assertEqual(cfg["deepseek"]["temperature"], 0.7)
        self.assertEqual(cfg["queue"]["debounce_seconds"], before["queue"]["debounce_seconds"])
        self.assertEqual(cfg["queue"]["generation_concurrency"], before["queue"]["generation_concurrency"])
        self.assertTrue(Path(cfg["queue"]["file"]).is_absolute())
        cfg["search"]["feedback_phrases"].append("实例改动")
        self.assertEqual(DEFAULTS, before)

    def test_defaults_file_accepts_bom_and_rejects_missing_or_invalid_document(self):
        path = self.root / "config.defaults.json"
        path.write_text('{"target":{"bot_name":"合成名字"}}', encoding="utf-8-sig")
        self.assertEqual(_load_defaults(path)["target"]["bot_name"], "合成名字")
        for body in (None, b'[]', b'{bad-json}', b'\xff\xfe'):
            with self.subTest(body=body):
                if body is None:
                    path.unlink()
                else:
                    path.write_bytes(body)
                with self.assertRaises(ValueError):
                    _load_defaults(path)

    def test_memory_thinking_defaults_overrides_and_invalid_values(self):
        with patch("wxbridge.config.PROJECT_ROOT", self.root), patch.dict(os.environ, {}, clear=True):
            cfg = load_config(self.config_file)
            self.assertEqual((cfg["memory"]["thinking"], cfg["memory"]["reasoning_effort"]), ("enabled", "max"))
            self.config_file.write_text(json.dumps({"memory": {"thinking": "disabled", "reasoning_effort": "low"}}), encoding="utf-8")
            cfg = load_config(self.config_file)
            self.assertEqual((cfg["memory"]["thinking"], cfg["memory"]["reasoning_effort"]), ("disabled", "low"))
            for key, value in (("thinking", "auto"), ("thinking", True), ("thinking", None),
                               ("reasoning_effort", "ultra"), ("reasoning_effort", 1), ("reasoning_effort", [])):
                with self.subTest(key=key, value=value):
                    self.config_file.write_text(json.dumps({"memory": {key: value}}), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "memory."+key):
                        load_config(self.config_file)


if __name__ == "__main__":
    unittest.main()
