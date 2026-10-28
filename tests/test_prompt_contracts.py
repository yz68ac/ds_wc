"""Named policy composition and media quote slots use detached synthetic input."""
from copy import deepcopy
import json
import pickle
import unittest
from unittest.mock import patch

import test_media as media_tests
from synthetic import NOW, msg
from wxbridge.ai.context import ContextError
from wxbridge.ai.images import prepare_multimodal
from wxbridge.ai.output import response_prompt
from wxbridge.ai.prompt_parts import PromptComposer, PromptMessages
from wxbridge.ai.prompts import build_prompt
from wxbridge.ai.search import search_prompt


class PromptContractTests(unittest.TestCase):
    setUp = media_tests.LocalImageTests.setUp
    generation = media_tests.LocalImageTests.generation

    def base(self, generation):
        return build_prompt(self.cfg, generation['context'], generation['triggers'][-1], generation['chat_title'],
                            generation['triggers'], memory=generation.get('memory'), account=generation.get('account', ''))

    def test_named_output_search_parts_are_idempotent_and_keep_tool_state(self):
        original = [dict(role='system', content='合成人格'), dict(role='user', content='普通提问'),
                    dict(role='assistant', content='', _provider_state={'opaque': ['signed-state']})]
        expected = deepcopy(original)
        once = search_prompt(self.cfg, original)
        twice = search_prompt(self.cfg, response_prompt(once, self.cfg))
        self.assertEqual(once, twice)
        self.assertEqual(twice[0]['content'].count('【群聊输出要求】'), 1)
        self.assertEqual(twice[0]['content'].count('【联网工具规则】'), 1)
        self.assertEqual(twice[-1]['_provider_state'], original[-1]['_provider_state'])
        self.assertEqual(original, expected)

    def test_spawn_pickle_keeps_layout_but_provider_json_contains_only_messages(self):
        base = self.base(self.generation())
        restored = pickle.loads(pickle.dumps(base))
        self.assertIsInstance(restored, PromptMessages)
        self.assertEqual(base, restored)
        self.assertEqual(base.layout, restored.layout)
        wire = json.dumps(restored, ensure_ascii=False)
        self.assertNotIn('trigger_keys', wire)
        self.assertNotIn('history_start', wire)

    def test_media_binding_does_not_rebuild_and_preserves_outside_additions(self):
        self.path.write_bytes(media_tests.png())
        generation = self.generation()
        base = self.base(generation)
        base[1]['content'] = '请求级前言\n'+base[1]['content']+'\n本轮额外要求'
        with patch('wxbridge.ai.prompts.build_prompt', side_effect=AssertionError('must not rebuild')):
            prompt, info = prepare_multimodal(self.cfg, base, generation)
        text = prompt[1]['content'][0]['text']
        self.assertTrue(text.startswith('请求级前言\n'))
        self.assertTrue(text.endswith('\n本轮额外要求'))
        self.assertIn('普通历史', text)
        self.assertIn('"available": true', text)
        self.assertEqual(info['images'], 1)
        self.assertFalse(generation['triggers'][0]['reference']['available'])

    def test_bound_layout_rejects_wrong_account_trigger_and_reference(self):
        generation = self.generation()
        generation['account'] = 'wxid_synthetic_bot'
        base = self.base(generation)
        with self.assertRaises(ContextError):
            PromptComposer.references(base, generation['triggers'], account='wxid_wrong')
        changed = deepcopy(generation['triggers'])
        changed[0]['local_id'] += 1
        with self.assertRaises(ContextError):
            PromptComposer.references(base, changed, account=generation['account'])
        changed = deepcopy(generation['triggers'])
        changed[0]['reference']['image_md5'] = 'b'*32
        with self.assertRaises(ContextError):
            PromptComposer.references(base, changed, account=generation['account'])

    def test_internal_layout_edit_fails_without_silently_losing_it(self):
        generation = self.generation()
        base = self.base(generation)
        base[1]['content'] = base[1]['content'].replace('普通历史', '外部改写的历史')
        with self.assertRaises(ContextError):
            PromptComposer.references(base, generation['triggers'], account='')
        self.assertIn('外部改写的历史', base[1]['content'])

    def test_media_growth_reduces_whole_memory_lines_before_history(self):
        self.path.write_bytes(media_tests.png())
        generation = self.generation()
        base_length = len(self.base(generation)[1]['content'])
        generation['memory'] = dict(text='第一条弱背景'+('甲'*120)+'\n第二条弱背景'+('乙'*120))
        self.cfg['target']['context_max_chars'] = base_length+390
        base = self.base(generation)
        self.assertIn('第二条弱背景', base[1]['content'])
        prompt, _ = prepare_multimodal(self.cfg, base, generation)
        text = prompt[1]['content'][0]['text']
        self.assertLessEqual(len(text), self.cfg['target']['context_max_chars'])
        self.assertIn('普通历史', text)
        self.assertIn('本次新写的提问', text)
        self.assertNotIn('第二条弱背景', text)
        self.assertNotIn('因预算未附送', text)

    def test_recent_media_budget_reduces_only_complete_old_history(self):
        self.path.write_bytes(media_tests.png())
        generation = self.generation()
        generation['context'] = [msg(7, NOW-3, '很早的历史'+('甲'*220)),
                                 msg(8, NOW-2, '较近的历史'+('乙'*220))]
        length = len(self.base(generation)[1]['content'])
        self.cfg['target'].update(context_budget_strategy='recent', context_max_chars=length+1)
        base = self.base(generation)
        prompt, _ = prepare_multimodal(self.cfg, base, generation)
        text = prompt[1]['content'][0]['text']
        self.assertLessEqual(len(text), self.cfg['target']['context_max_chars'])
        self.assertNotIn('很早的历史', text)
        self.assertIn('较近的历史', text)
        self.assertIn('因预算未附送', text)
        self.assertIn('@ds 这怎么看', text)

    def test_plain_list_input_keeps_legacy_compatibility(self):
        self.path.write_bytes(media_tests.png())
        generation = self.generation()
        prompt, info = prepare_multimodal(self.cfg, list(self.base(generation)), generation)
        self.assertEqual(info['images'], 1)
        self.assertIn('普通历史', prompt[1]['content'][0]['text'])
