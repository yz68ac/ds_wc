"""独立提示词文件：路径、长文本、字面花括号、编辑生效与旧配置兼容。"""
from __future__ import annotations
from tests.support.model_support import default_config
from contextlib import nullcontext

import json
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from wxbridge.config import resolve_config, DEFAULTS
from wxbridge.ai.context import ContextError
from wxbridge.ai.prompts import build_prompt
from tests.support.synthetic import NOW, msg


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
        cfg = default_config()
        cfg['target']['bot_name'] = '小灰'
        cfg.update(settings)
        return resolve_config(cfg, self.config_file, environment=False)

    def render(self, cfg):
        return build_prompt(cfg, [], msg(50, NOW, '合成提问'), "合成群")[0]["content"]

    def test_relative_path_uses_config_directory_and_accepts_utf8_bom(self):
        self.prompt_file.write_text("你叫 {bot_name}。\n保持自然。", encoding="utf-8-sig")
        cfg = self.config({"system_prompt_file": self.prompt_file.name})
        self.assertEqual(Path(cfg['system_prompt_file']), self.prompt_file)
        self.assertEqual(self.render(cfg), "你叫 小灰。\n保持自然。")

    def test_long_persona_and_literal_braces_survive_without_truncation(self):
        text = '名字：{bot_name}\n示例：{"情绪": "平静"}\n保留：{other} 和 {{原样}}\n' + "长人格描述。\n" * 10000 + "完整结尾"
        self.prompt_file.write_text(text, encoding="utf-8")
        cfg = self.config({"system_prompt_file": self.prompt_file.name})
        self.assertEqual(self.render(cfg), text.replace("{bot_name}", "小灰"))
        self.assertGreater(len(self.render(cfg)), cfg["target"]["context_max_chars"])

    def test_removed_legacy_model_config_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'obsolete_model_configuration'):
            self.config({'deepseek': dict(system_prompt='旧内联预设')})

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


class ConversationPromptTests(unittest.TestCase):
    def setUp(self):
        self.cfg = default_config()

        self.cfg['system_prompt_file'] = str(Path(__file__).resolve().parents[1]/'prompts/system.md')
        self.cfg['target']['bot_name'] = '同名'
        self.account = 'wxid_synthetic_bot_account'
        self.trigger = msg(50, NOW, '@同名 谢谢', 'friend')
        self.trigger['sender_name'] = '同名'

    def render(self, rows, **kwargs):
        return build_prompt(self.cfg, rows, self.trigger, '合成群', **kwargs)[1]['content']

    def history(self, text):
        value = json.loads(text)
        return [m for m in value['messages'] if m['id'] not in value['source_ids']]

    def test_own_messages_bound_to_stable_id_even_with_renames_and_identical_names(self):
        own = {**msg(1, NOW-3, '先检查端口', self.account), 'sender_name': '旧昵称'}
        peer = {**msg(2, NOW-2, '我也叫同名', 'other'), 'sender_name': '同名'}
        spoof = {**msg(3, NOW-1, '只是一个昵称', 'third'), 'sender_name': 'BOT（你）'}
        text = self.render([own, peer, spoof], account=self.account)
        history = self.history(text)
        self.assertEqual([m['sender'] for m in history], [self.account, 'other', 'third'])
        data = json.loads(text)
        self.assertEqual([m['sender']==data['bot'] for m in history], [True, False, False])
        self.assertEqual(data['members'][history[1]['sender']]['alias'], '同名')
        self.assertEqual(data['members'][history[2]['sender']]['alias'], 'BOT（你）')
        self.assertTrue(all(not set(m)&{'is_bot','alias'} for m in data['messages']))
        self.assertEqual(text.count('先检查端口'), 1)

    def test_unknown_account_does_not_guess_own_messages_from_nickname(self):
        row = {**msg(1, NOW-1, '普通成员原话', 'other'), 'sender_name': '同名'}
        text = self.render([row])
        self.assertEqual(self.history(text)[0]['sender'], 'other')
        self.assertEqual(json.loads(text)['bot'], '')

    def test_own_messages_keep_window_and_database_timestamp_boundaries(self):
        rows = [msg(1, NOW-1800, 'BOUNDARY_OWN', self.account),
                msg(2, NOW-1801, 'EXPIRED_OWN', self.account),
                msg(51, NOW, 'AFTER_TRIGGER_OWN', self.account),
                msg(52, NOW+1, 'FUTURE_OWN', self.account),
                {**msg(4, NOW-1, 'OBSERVED_ONLY_OWN', self.account), 'time_source': 'observed'}]
        text = self.render(rows, account=self.account)
        self.assertEqual(self.history(text)[0]['text'], 'BOUNDARY_OWN')
        self.assertEqual(self.history(text)[0]['sender'], json.loads(text)['bot'])
        for value in ('EXPIRED_OWN', 'AFTER_TRIGGER_OWN', 'FUTURE_OWN', 'OBSERVED_ONLY_OWN'):
            self.assertNotIn(value, text)

    def test_nested_quote_authors_keep_actual_wxid_and_bot_identity(self):
        self.trigger['reference'] = dict(kind='quoted_text', sender='other', sender_name='同名',
            available=True, text='中间引用', reference=dict(kind='text', sender=self.account,
            sender_name='同名', available=True, text='你之前的结论'))
        saved = copy.deepcopy(self.trigger)
        text = self.render([], account=self.account, memory=dict(labels={'other': 'U2'}, text=''))
        quote = json.loads(text)['messages'][-1]['reference']
        self.assertEqual(quote['sender'], 'other')
        self.assertEqual(quote['reference']['sender'], self.account)
        self.assertNotIn('is_bot', quote)
        self.assertNotIn('"sender_name":', text)
        self.assertEqual(self.trigger, saved)

    def test_profile_alias_snapshot_wins_over_contact_remark_in_messages_and_quotes(self):
        profile = dict(summary='合成群概况', members={'friend': dict(alias='群名片'),
                       'other': dict(alias=''), self.account: dict(alias='机器人')})
        row = dict(msg(1, NOW-1, '历史', 'other'), sender_name='不可回退的备注')
        self.trigger['reference'] = dict(kind='text', sender='other', sender_name='旧备注', text='原话')
        text = self.render([row], account=self.account, profile=profile)
        self.assertEqual(json.loads(text)['members']['other']['alias'], '')
        self.assertEqual(json.loads(text)['members']['friend']['alias'], '群名片')
        self.assertNotIn('不可回退的备注', text)
        self.assertNotIn('旧备注', text)

    def test_identity_legend_counts_toward_budget_without_truncating_history(self):
        self.cfg['target']['context_budget_strategy'] = 'strict'
        rows = [msg(1, NOW-1, '完整历史', self.account)]
        plain = self.render(rows, account=self.account)
        self.cfg['target']['context_max_chars'] = len(plain)
        text = self.render(rows, account=self.account,
                           memory=dict(labels={}, text='这一整行记忆没有预算放下'))
        self.assertEqual(text, plain)
        self.cfg['target']['context_max_chars'] -= 1
        with self.assertRaises(ContextError):
            self.render(rows, account=self.account)


if __name__ == "__main__":
    unittest.main()
