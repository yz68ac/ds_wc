"""Identity configuration, legacy migration and name-trigger regressions."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from wxbridge.ai.prompts import system_prompt
from wxbridge.ai.proactive import address_names, addresses_bot, build_prompt
from wxbridge.config import load_config


class PersonaConfigTests(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parents[1] / '.test-tmp'
        parent.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / 'config.json'

    def load(self, value):
        self.config.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        with patch('wxbridge.config.load_dotenv'), patch.dict('os.environ', {}, clear=True):
            return load_config(self.config)

    def test_legacy_config_still_matches_all_previous_names(self):
        cfg = self.load({'target': {'bot_name': 'old', 'trigger': {'mentions': ['@Legacy']}},
                         'proactive': {'opportunities': {'address_names': ['ds']}}})
        self.assertEqual(address_names(cfg), ['old', 'ds', 'Legacy'])
        for name in ('old', 'DS', 'Legacy'):
            self.assertTrue(addresses_bot({'text': '叫'+name+'看看'}, cfg, 'self'))
        self.assertFalse(addresses_bot({'text': 'ads'}, cfg, 'self'))

    def test_explicit_aliases_replace_legacy_names_but_not_real_mentions(self):
        cfg = self.load({'persona': {'name': '橙子', 'aliases': ['Orange', '小D']},
                         'target': {'bot_name': 'old', 'trigger': {'mentions': ['@Legacy']}},
                         'proactive': {'opportunities': {'address_names': ['ds']}}})
        self.assertEqual(address_names(cfg), ['橙子', 'Orange', '小D'])
        for name in ('橙子', 'ORANGE', '小D'):
            self.assertTrue(addresses_bot({'text': '叫'+name+'看看'}, cfg, 'self'))
        for name in ('old', 'Legacy', 'ds', 'Oranges'):
            self.assertFalse(addresses_bot({'text': name}, cfg, 'self'))
        self.assertFalse(addresses_bot({'text': '橙子怎么看', 'mentioned_users': ['other']}, cfg, 'self'))
        self.assertTrue(addresses_bot({'text': '怎么看', 'reference': {'sender': 'self'}}, cfg, 'self'))
        self.assertEqual(cfg['target']['trigger']['mentions'], ['@Legacy'])

    def test_partial_persona_fields_and_inline_prompt_precedence(self):
        base = {'target': {'bot_name': 'old'}, 'deepseek': {'system_prompt': 'hello {bot_name}'}}
        cfg = self.load({**base, 'persona': {'name': '新名'}})
        self.assertEqual(system_prompt(cfg), 'hello 新名')
        self.assertIn('ds', address_names(cfg))
        prompt = self.root / 'persona.md'
        prompt.write_text('文件人格 {bot_name}', encoding='utf-8')
        cfg = self.load({**base, 'persona': {'name': '新名', 'prompt_file': 'persona.md'}})
        self.assertEqual(cfg['deepseek']['system_prompt_file'], str(prompt.resolve()))
        self.assertEqual(system_prompt(cfg), '文件人格 新名')

    def test_judgment_prompt_uses_current_names_without_hardcoded_ds(self):
        cfg = self.load({'persona': {'name': '橙子', 'aliases': ['小D']}})
        prompt = build_prompt(cfg, [], 'test')[0]['content']
        self.assertIn('橙子', prompt)
        self.assertIn('小D', prompt)
        self.assertNotIn('出现ds', prompt.lower())
        self.assertNotIn('让ds来评评', prompt.lower())
        self.assertIn('优先结合上下文自然回应', prompt)

    def test_invalid_configuration_is_rejected(self):
        for value in ([], 'name', {'unknown': 'x'}, {'name': ''}, {'name': 'x\ny'},
                      {'aliases': ['']}, {'aliases': 'ds'}, {'aliases': ['x'] * 11},
                      {'prompt_file': ''}, {'prompt_file': None}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'persona'):
                self.load({'persona': value})


if __name__ == '__main__':
    unittest.main()
