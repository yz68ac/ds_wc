"""独立提示词文件：路径、长文本、字面花括号、编辑生效与旧配置兼容。"""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from wxbridge.config import load_config
from wxbridge.ai.context import ContextError
from wxbridge.ai.prompts import build_prompt


class PromptFileTests(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parents[1] / ".test-tmp"
        parent.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        project_root = patch("wxbridge.config.PROJECT_ROOT", self.root)
        project_root.start()
        self.addCleanup(project_root.stop)
        self.config_file = self.root / "config.json"
        self.prompt_file = self.root / "人格设定.md"
        environment = patch.dict("os.environ", {"WX_GROUP": "", "WX_DB_DIR": "", "DEEPSEEK_MODEL": "", "DEEPSEEK_BASE_URL": ""})
        environment.start()
        self.addCleanup(environment.stop)

    def config(self, settings):
        self.config_file.write_text(json.dumps({
            "target": {"bot_name": "小灰"}, "deepseek": settings,
        }, ensure_ascii=False), encoding="utf-8")
        return load_config(self.config_file)

    def render(self, cfg):
        return build_prompt(cfg, [], {"time": 1700000600, "text": "合成提问"}, "合成群")[0]["content"]

    def test_relative_path_uses_config_directory_and_accepts_utf8_bom(self):
        self.prompt_file.write_text("你叫 {bot_name}。\n保持自然。", encoding="utf-8-sig")
        cfg = self.config({"system_prompt_file": self.prompt_file.name})
        self.assertEqual(Path(cfg["deepseek"]["system_prompt_file"]), self.prompt_file)
        self.assertEqual(self.render(cfg), "你叫 小灰。\n保持自然。")

    def test_long_persona_and_literal_braces_survive_without_truncation(self):
        text = '名字：{bot_name}\n示例：{"情绪": "平静"}\n保留：{other} 和 {{原样}}\n' + "长人格描述。\n" * 10000 + "完整结尾"
        self.prompt_file.write_text(text, encoding="utf-8")
        cfg = self.config({"system_prompt_file": self.prompt_file.name})
        self.assertEqual(self.render(cfg), text.replace("{bot_name}", "小灰"))
        self.assertGreater(len(self.render(cfg)), cfg["target"]["context_max_chars"])

    def test_legacy_inline_config_keeps_working(self):
        cfg = self.config({"system_prompt": "内联身份：{bot_name}，示例 {\"x\":1}"})
        self.assertFalse(cfg["deepseek"]["system_prompt_file"])
        self.assertEqual(self.render(cfg), '内联身份：小灰，示例 {"x":1}')

    def test_invalid_file_cancels_instead_of_falling_back_to_inline(self):
        cfg = self.config({"system_prompt_file": self.prompt_file.name, "system_prompt": "不可静默回退的人设"})
        for body in (None, b"  \n", b"\xff\xfe"):
            with self.subTest(body=body):
                if body is not None:
                    self.prompt_file.write_bytes(body)
                with self.assertRaises(ContextError):
                    self.render(cfg)

    def test_edits_are_loaded_on_next_prompt_without_changing_config(self):
        cfg = self.config({"system_prompt_file": self.prompt_file.name, "system_prompt": "旧内联内容"})
        self.prompt_file.write_text("第一版人设", encoding="utf-8")
        self.assertEqual(self.render(cfg), "第一版人设")
        self.prompt_file.write_text("第二版人设", encoding="utf-8")
        self.assertEqual(self.render(cfg), "第二版人设")


if __name__ == "__main__":
    unittest.main()
