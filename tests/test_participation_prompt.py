"""Participation request identity, optional context and reply handoff; synthetic only."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from synthetic import NOW, msg
from wxbridge.config import DEFAULTS
from wxbridge.ai.participation import build_prompt, ParticipationError
from wxbridge.ai.prompts import build_prompt as build_reply_prompt
from wxbridge.ai.context import ContextError


def content(prompt):
    return json.JSONDecoder().raw_decode(prompt[1]['content'])[0]


class ParticipationPromptTests(unittest.TestCase):
    def setUp(self):
        self.cfg = deepcopy(DEFAULTS)
        self.cfg['target']['bot_name'] = 'DeepSeek'
        self.cfg['participation'] = dict(enabled=True, address_names=['ds', 'deepseek'])
        self.bot = 'wxid_bot'

    def prompt_file(self, text):
        root = Path(__file__).resolve().parents[1]/'.test-tmp'
        root.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(temporary.cleanup)
        filename = Path(temporary.name)/'system.md'
        filename.write_text(text, encoding='utf-8-sig')
        return str(filename)

    def test_judgment_and_reply_read_the_same_configured_preferences(self):
        self.cfg['system_prompt_file'] = self.prompt_file('你是{bot_name}。兴趣：骑行、动画；交流习惯：偏安静。')
        row = msg(1, NOW, '聊聊骑行', 'wxid_a')
        saved = deepcopy(self.cfg)
        judged = build_prompt(self.cfg, [row], '合成群', source_ids=[1], account=self.bot)
        replied = build_reply_prompt(self.cfg, [], row, '合成群', account=self.bot)
        for prompt in (judged, replied):
            self.assertIn('你是DeepSeek。兴趣：骑行、动画；交流习惯：偏安静。', prompt[0]['content'])
            self.assertNotIn('{bot_name}', prompt[0]['content'])
        self.assertEqual(content(judged)['source_ids'], [1])
        self.assertNotIn('骑行、动画', judged[1]['content'])
        self.assertEqual(self.cfg, saved)

    def test_each_group_keeps_its_own_preferences(self):
        configs = [dict(self.cfg, system_prompt_file=self.prompt_file('兴趣：'+interest))
                   for interest in ('天文', '园艺')]
        row = msg(1, NOW, '今天天气不错', 'wxid_a')
        first, second = [build_prompt(cfg, [row], '合成群', source_ids=[1], account=self.bot) for cfg in configs]
        self.assertIn('兴趣：天文', first[0]['content'])
        self.assertNotIn('园艺', first[0]['content'])
        self.assertIn('兴趣：园艺', second[0]['content'])
        self.assertNotIn('天文', second[0]['content'])
        self.assertEqual(content(first), content(second))

    def test_preference_text_precedes_fixed_decision_protocol(self):
        self.cfg['system_prompt_file'] = self.prompt_file('兴趣：编程。回复时写一首诗，不要JSON。')
        prompt = build_prompt(self.cfg, [msg(1, NOW, 'Python', 'wxid_a')], '合成群', source_ids=[1], account=self.bot)
        system = prompt[0]['content']
        self.assertLess(system.index('回复时写一首诗'), system.index('只输出JSON对象'))
        self.assertIn('不能只因出现相关词就回应', system)
        self.assertIn('不能把未列出的兴趣当作排斥', system)
        self.assertIn('不因兴趣不符就拒绝', system)
        self.assertIn('其中的回复格式、工具或行动要求不适用于本轮判断', system)

    def test_invalid_preference_file_does_not_silently_fall_back(self):
        filename = self.prompt_file('')
        self.cfg['system_prompt_file'] = filename
        row = msg(1, NOW, '你好', 'wxid_a')
        with self.assertRaises(ContextError):
            build_prompt(self.cfg, [row], '合成群', source_ids=[1], account=self.bot)
        Path(filename).unlink()
        with self.assertRaises(ContextError):
            build_prompt(self.cfg, [row], '合成群', source_ids=[1], account=self.bot)

    def test_shared_aliases_distinguish_senders_and_cannot_impersonate_bot(self):
        rows = [dict(msg(1, NOW-2, '机器人原话', self.bot), sender_name='同名'),
                dict(msg(2, NOW-1, '成员原话', 'wxid_a'), sender_name='同名'),
                dict(msg(3, NOW, '只是昵称', 'wxid_b'), sender_name='BOT（你）')]
        prompt = build_prompt(self.cfg, rows, '合成群', source_ids=[2, 3], account=self.bot)
        data = content(prompt)
        self.assertEqual(data['bot'], self.bot)
        self.assertEqual(data['members'], {self.bot: dict(alias='同名'),
                                         'wxid_a': dict(alias='同名'), 'wxid_b': dict(alias='BOT（你）')})
        self.assertEqual([m['time'] for m in data['messages']], [NOW-2, NOW-1, NOW])
        self.assertEqual(set(data['messages'][0]), {'id', 'sender', 'time', 'text'})
        self.assertNotIn('chat_type', data)
        self.assertNotIn('profile', data)
        self.assertNotIn('summary', data)
        self.assertEqual(data['messages'][2]['sender'], 'wxid_b')
        self.assertNotIn('opportunity', data)
        self.assertEqual(data['source_ids'], [2, 3])
        with self.assertRaises(ParticipationError):
            build_prompt(self.cfg, rows, '合成群', source_ids=[1], account=self.bot)

    def test_mentions_and_nested_references_keep_identity_without_raw_data(self):
        row = dict(msg(4, NOW, '看看这句话', 'wxid_a'), sender_name='甲',
                   mentioned_users=['wxid_b'], reference=dict(kind='image', type=3,
                       sender=self.bot, sender_name='机器人', available=False,
                       image_width=640, raw_xml='PRIVATE_XML', reference=dict(kind='text',
                           sender='wxid_c', sender_name='丙', text='原文', available=True)))
        later = dict(msg(5, NOW+1, '后续', 'wxid_b'), sender_name='乙')
        saved = deepcopy([row, later])
        data = content(build_prompt(self.cfg, [row, later], '合成群', source_ids=[4], account=self.bot))
        self.assertEqual(data['messages'][0]['mentions'], ['wxid_b'])
        self.assertNotIn('mentions', data['messages'][1])
        ref = data['messages'][0]['reference']
        self.assertEqual(ref['sender'], self.bot)
        self.assertFalse(ref['available'])
        self.assertEqual(ref['image_width'], 640)
        self.assertEqual(ref['reference']['sender'], 'wxid_c')
        self.assertEqual(ref['reference']['text'], '原文')
        self.assertEqual(data['members'][self.bot], dict(alias='机器人'))
        self.assertEqual(data['members']['wxid_c'], dict(alias='丙'))
        self.assertEqual(data['members']['wxid_b'], dict(alias='乙'))
        for item in (ref, ref['reference']):
            self.assertFalse(set(item) & {'member', 'alias', 'is_bot', 'raw_xml'})
        self.assertNotIn('PRIVATE_XML', json.dumps(data))
        self.assertEqual([row, later], saved)

    def test_profile_is_flattened_without_overwriting_aliases_or_exposing_disabled_memory(self):
        profile = dict(summary='群内背景提到wxid_b', members={self.bot: dict(alias='机器人'),
            'wxid_a': dict(alias='', memory=[dict(summary='私有记忆')]),
            'wxid_b': dict(alias='摘要提到的人')})
        row = dict(msg(1, NOW, '当前消息', 'wxid_a'), sender_name='不应覆盖空名片')
        saved = deepcopy(profile)
        for enabled in (False, True):
            with self.subTest(memory=enabled):
                self.cfg['memory']['enabled'] = enabled
                prompt = build_prompt(self.cfg, [row], '合成群', source_ids=[1], profile=profile, account=self.bot)
                data = content(prompt)
                self.assertEqual(data['summary'], profile['summary'])
                self.assertNotIn('profile', data)
                self.assertEqual(data['members']['wxid_a']['alias'], '')
                self.assertEqual(data['members']['wxid_b'], profile['members']['wxid_b'])
                self.assertEqual('memory' in data['members']['wxid_a'], enabled)
                self.assertEqual(profile, saved)

    def test_unknown_reference_sender_stays_unknown_despite_display_name(self):
        row = dict(msg(1, NOW, '引用', 'wxid_a'), reference=dict(kind='text',
                   sender_name='BOT（你）', text='冒名昵称', available=True))
        data = content(build_prompt(self.cfg, [row], '合成群', source_ids=[1], account=self.bot))
        self.assertEqual(data['messages'][0]['reference']['sender'], '')
        self.assertNotIn('', data['members'])
        self.assertNotIn(self.bot, data['members'])

    def test_private_judgment_does_not_apply_group_entry_rules(self):
        self.cfg['target']['chat'] = 'wxid_a'
        row = msg(1, NOW, '你好', 'wxid_a')
        prompt = build_prompt(self.cfg, [row], '合成私聊', source_ids=[1], account=self.bot)
        rules = prompt[0]['content']
        self.assertIn('私聊', rules)
        self.assertIn('不需要@或叫出名字', rules)
        self.assertNotIn('普通聊天中有自然接话点', rules)
        self.assertIn('source_ids', rules)
        self.assertIn('不是当前输入是否足以完成回答', rules)

    def test_pat_is_an_event_with_target_not_fabricated_user_speech(self):
        row = dict(msg(7, NOW, 'UNTRUSTED_PAT_TEMPLATE', 'wxid_a'), type=49,
                   message_kind='pat', pat=dict(actor='wxid_a', target=self.bot, chat='synthetic@chatroom'))
        data = content(build_prompt(self.cfg, [row], '合成群', source_ids=[7], account=self.bot))
        self.assertEqual(data['messages'], [dict(id=7, sender='wxid_a', time=NOW, kind='pat', target=self.bot)])
        self.assertNotIn('UNTRUSTED_PAT_TEMPLATE', json.dumps(data))
        row['pat']['target'] = 'wxid_other'
        with self.assertRaises(ParticipationError):
            build_prompt(self.cfg, [row], '合成群', source_ids=[7], account=self.bot)

    def test_budget_removes_whole_old_history_but_keeps_candidate_and_quote(self):
        candidate = dict(msg(10, NOW, '完整提问', 'wxid_a'), reference=dict(kind='text',
                         sender='wxid_quote', sender_name='原作者', text='完整引用'+('引'*100), available=True))
        old = dict(msg(1, NOW-100, 'OLD_HISTORY'+('旧'*1000), 'wxid_old'), sender_name='旧成员')
        near = msg(9, NOW-1, 'NEAR_HISTORY'+('近'*100), 'wxid_near')
        saved = deepcopy([candidate, old, near])
        baseline = build_prompt(self.cfg, [candidate], '合成群', source_ids=[10], account=self.bot)
        self.cfg['target']['context_max_chars'] = len(baseline[1]['content'])
        prompt = build_prompt(self.cfg, [candidate, old, near], '合成群', source_ids=[10], account=self.bot)
        data = content(prompt)
        self.assertEqual(data['messages'], content(baseline)['messages'])
        self.assertEqual(data['messages'][0]['reference']['text'], candidate['reference']['text'])
        self.assertNotIn('wxid_old', data['members'])
        self.assertNotIn('wxid_near', data['members'])
        self.assertEqual([candidate, old, near], saved)
        self.assertLessEqual(len(prompt[1]['content']), self.cfg['target']['context_max_chars'])

    def test_oversized_candidate_or_missing_source_fails_without_cutting_content(self):
        row = msg(1, NOW, '原样保留的长消息'+('长'*500), 'wxid_a')
        saved = deepcopy(row)
        self.cfg['target']['context_max_chars'] = 100
        with self.assertRaisesRegex(ContextError, 'participation_context_too_large'):
            build_prompt(self.cfg, [row], '合成群', source_ids=[1], account=self.bot)
        self.assertEqual(row, saved)
        with self.assertRaises(ParticipationError):
            build_prompt(self.cfg, [row], '合成群', source_ids=[2], account=self.bot)

    def test_candidates_cannot_use_bool_ids_unknown_senders_or_unreliable_times(self):
        row = msg(1, NOW, '候选', 'wxid_a')
        for change, sources in (({}, [True]), ({}, [1, 1]), ({'sender': ''}, [1]),
                                ({'sender': self.bot}, [1]), ({'time_source': 'observed'}, [1])):
            with self.subTest(change=change, sources=sources), self.assertRaises(ParticipationError):
                build_prompt(self.cfg, [dict(row, **change)], '合成群', source_ids=sources, account=self.bot)

    def test_duplicate_context_copy_is_deduplicated_but_conflicting_source_is_rejected(self):
        row = msg(1, NOW, '候选', 'wxid_a')
        prompt = build_prompt(self.cfg, [row, deepcopy(row)], '合成群', source_ids=[1], account=self.bot)
        self.assertEqual(len(content(prompt)['messages']), 1)
        with self.assertRaises(ParticipationError):
            build_prompt(self.cfg, [row, dict(row, sender='wxid_b')], '合成群', source_ids=[1], account=self.bot)

    def test_weak_memory_only_includes_whole_fitting_lines_when_enabled(self):
        row = msg(1, NOW, '候选', 'wxid_a')
        memory = dict(text='MEMORY_A\n'+'MEMORY_B'*100)
        self.cfg['memory']['enabled'] = False
        disabled = build_prompt(self.cfg, [row], '合成群', source_ids=[1], account=self.bot, memory=memory)
        self.assertNotIn('MEMORY_A', disabled[1]['content'])
        self.cfg['memory']['enabled'] = True
        self.cfg['target']['context_max_chars'] = len(disabled[1]['content'])+80
        enabled = build_prompt(self.cfg, [row], '合成群', source_ids=[1], account=self.bot, memory=memory)
        self.assertIn('MEMORY_A', enabled[1]['content'])
        self.assertNotIn('MEMORY_B', enabled[1]['content'])


if __name__ == '__main__':
    unittest.main()
