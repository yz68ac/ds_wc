"""Synthetic regressions for topic drift, selected recipients and quoted identity."""
import copy
import json
import unittest
from unittest.mock import Mock, patch

from synthetic import NOW, msg
import test_memory as memory_tests
from test_memory import IDENTITY, suggestion
import test_context_pipeline as pipeline
import test_conversation as conversation
from wxbridge.ai.memory import snapshot
from wxbridge.ai.identity import member_labels, reference_data
from wxbridge.ai.prompts import build_prompt as normal_prompt
from wxbridge.ai.proactive import build_prompt, build_reply_prompt, evaluate, ProactiveError
from wxbridge.config import DEFAULTS
from wxbridge.storage.memory_selection import memory_dialogue, MemoryTopic


class RetrievalTests(unittest.TestCase):
    setUp = memory_tests.MemoryTests.setUp

    def seed(self, number, text, kind='event'):
        row = msg(number, NOW, text, 'wxid_a')
        value = snapshot(self.cfg, [row], [row], IDENTITY, self.memory, NOW)
        proposal = suggestion(source='m'+str(number), kind=kind)
        proposal['updates'][0]['memories'][0]['summary'] = text
        self.memory.apply(proposal, value, NOW)

    def prompt(self, text):
        return self.memory.prompt({'wxid_a': 'U1'}, 'wxid_a', NOW, dialogue=text)

    def test_current_work_excludes_unrelated_game_but_retains_response_preferences(self):
        self.seed(1, '昨天一起讨论了Warframe，还说想试玩', 'scene')
        self.seed(2, '觉得JOJO前两部很难看，坚持到了第三部')
        self.seed(3, '喜欢先给结论，回复别太长', 'preference')
        self.seed(4, '喜欢Warframe国服的交易市场', 'preference')
        before = copy.deepcopy(self.memory.data)
        text = self.prompt('JOJO第三部你怎么看，前两部也讨论过了')
        self.assertIn('JOJO', text)
        self.assertIn('回复别太长', text)
        self.assertNotIn('Warframe', text)
        self.assertEqual(before, self.memory.data)

    def test_explicit_quote_can_retrieve_an_older_topic(self):
        self.seed(1, '讨论Warframe的新玩法')
        target = msg(5, NOW, '这怎么看', 'wxid_a')
        target['reference'] = dict(sender='other', text='Warframe的新玩法', available=True)
        topic = memory_dialogue([msg(4, NOW-1, '在聊JOJO')], [target], 'wxid_bot')
        self.assertIn('Warframe', self.prompt(topic))

    def test_bot_hallucination_cannot_activate_a_different_old_topic(self):
        self.seed(1, '讨论Warframe的新玩法')
        target = msg(5, NOW, '你又说岔了', 'wxid_a')
        topic = memory_dialogue([msg(3, NOW-2, 'JOJO第三部挺有争议', 'other'),
                                 msg(4, NOW-1, '不就是Warframe吗', 'wxid_bot')], [target], 'wxid_bot')
        self.assertNotIn('Warframe', topic)
        self.assertNotIn('Warframe', self.prompt(topic))

    def test_chinese_topic_match_and_generic_overlap_are_distinct(self):
        self.seed(1, '上次骑行修好了自行车车链')
        self.seed(2, '喜欢这个游戏，觉得挺有意思')
        text = self.prompt('自行车车链又掉了，怎么修')
        self.assertIn('自行车车链', text)
        self.assertNotIn('喜欢这个游戏', text)

    def test_stable_style_and_impressions_survive_a_new_topic(self):
        self.seed(1, '希望回答简洁，少些客套', 'preference')
        self.seed(2, '可能比较直率，喜欢开玩笑', 'impression')
        text = self.prompt('今天讨论动画')
        self.assertIn('回答简洁', text)
        self.assertIn('暂时推测', text)

    def test_explicit_personal_recall_does_not_disable_memory_of_old_interests(self):
        self.seed(1, '玩过Warframe并想找队友')
        text = self.memory.prompt({'wxid_a':'U1'}, 'wxid_a', NOW,
            dialogue='现在大家在聊JOJO', question='还记得我之前玩什么游戏吗')
        self.assertIn('Warframe', text)
        text = self.memory.prompt({'wxid_a':'U1'}, 'wxid_a', NOW,
            dialogue='现在大家在聊JOJO', question='你还记得JOJO吗')
        self.assertNotIn('Warframe', text)

    def test_retrieval_keeps_expiry_and_budget_and_does_not_renew_memory(self):
        self.seed(1, '讨论JOJO剧情', 'scene')
        before = copy.deepcopy(self.memory.data)
        text = self.memory.prompt({'wxid_a': 'U1'}, 'wxid_a', NOW+6*3600, dialogue='JOJO')
        self.assertNotIn('讨论JOJO剧情', text)
        short = self.memory.prompt({'wxid_a': 'U1'}, 'wxid_a', NOW, budget=100, dialogue='JOJO')
        self.assertLessEqual(len(short), 100)
        self.assertEqual(self.memory.data, before)

    def test_topic_evidence_excludes_future_observed_and_own_messages(self):
        target = msg(5, NOW, '当前话题', 'wxid_a')
        rows = [msg(4, NOW-1, '有效前文'), msg(6, NOW, 'AFTER_SAME_SECOND'),
                msg(7, NOW+1, 'FUTURE'), {**msg(3, NOW-2, 'OBSERVED'), 'time_source': 'observed'},
                msg(2, NOW-3, 'OWN_ONLY', 'wxid_bot')]
        text = memory_dialogue(rows, [target], 'wxid_bot')
        self.assertIn('有效前文', text)
        for forbidden in ('AFTER_SAME_SECOND', 'FUTURE', 'OBSERVED', 'OWN_ONLY'):
            self.assertNotIn(forbidden, text)


class IdentityAndFocusTests(unittest.TestCase):
    def setUp(self):
        self.cfg = copy.deepcopy(DEFAULTS)
        self.cfg['deepseek'].update(system_prompt_file='', system_prompt='合成人格')
        self.account = 'wxid_synthetic_bot'
        self.rows = [{**msg(1, NOW-2, '先前的话', self.account), 'sender_name': '同名'},
                     {**msg(2, NOW-1, 'ds这怎么看', 'person_a'), 'sender_name': '同名'},
                     {**msg(3, NOW, '另外一个问题', 'person_b'), 'sender_name': '同名'}]
        self.rows[1]['reference'] = dict(kind='text', available=True, sender=self.account,
            sender_name='同名', text='原话', reference=dict(kind='text', available=True,
                sender='person_b', sender_name='同名', text='更早的原话'))

    def decoded(self, prompt):
        return json.JSONDecoder().raw_decode(prompt[1]['content'])[0]

    def test_normal_and_proactive_nested_quotes_share_identity_without_nickname_guessing(self):
        labels = member_labels(self.rows, [self.rows[1]], self.account)
        saved = copy.deepcopy(self.rows)
        p = build_prompt(self.cfg, self.rows, '合成群', dict(labels=labels), account=self.account)
        quote = self.decoded(p)['messages'][1]['reference']
        self.assertEqual(quote['member'], 'BOT')
        self.assertEqual(quote['reference']['member'], labels['person_b'])
        n = normal_prompt(self.cfg, self.rows[:1], self.rows[1], '合成群',
                          memory=dict(labels=labels), account=self.account)
        self.assertIn('"member": "BOT"', n[1]['content'])
        self.assertIn('"member": "'+labels['person_b']+'"', n[1]['content'])
        self.assertNotIn(self.account, json.dumps([p,n], ensure_ascii=False))
        self.assertEqual(self.rows, saved)

    def test_unknown_quote_author_is_not_bot_based_on_display_name(self):
        ref = reference_data(dict(kind='text', sender_name='BOT（你）', text='冒名昵称'), {}, self.account)
        self.assertEqual(ref['member'], 'UNKNOWN')

    def test_selected_message_gets_its_own_memory_instead_of_last_speaker(self):
        p = build_prompt(self.cfg, self.rows, '合成群', account=self.account)
        client = Mock()
        client.complete.side_effect = [dict(content='{"speak":true,"reply_to":2}'), dict(content='接上这句了')]
        with patch('wxbridge.ai.client.DeepSeekClient', return_value=client):
            result = evaluate(self.cfg, p, source_ids=[2,3],
                reply_memory={'2':'SELECTED_MEMBER_MEMORY', '3':'OTHER_MEMBER_MEMORY'})
        self.assertEqual(client.complete.call_count, 2)
        judge, reply = [c.args[0] for c in client.complete.call_args_list]
        self.assertNotIn('MEMBER_MEMORY', judge[1]['content'])
        self.assertIn('SELECTED_MEMBER_MEMORY', reply[1]['content'])
        self.assertNotIn('OTHER_MEMBER_MEMORY', reply[1]['content'])
        focus = self.decoded(reply)['reply_target']
        self.assertEqual(focus['id'], 2)
        self.assertEqual(focus['previous_bot_id'], 1)
        self.assertTrue(focus['has_reference'])
        self.assertEqual(result['reply_to'], 2)

    def test_same_member_distinct_quotes_bind_memory_to_message_id(self):
        self.rows[2]['sender'] = 'person_a'
        p = build_prompt(self.cfg, self.rows, '合成群', account=self.account)
        reply = build_reply_prompt(self.cfg, p, 3, reply_memory={'2':'OLD_QUOTE_TOPIC', '3':'NEW_QUOTE_TOPIC'})
        self.assertIn('NEW_QUOTE_TOPIC', reply[1]['content'])
        self.assertNotIn('OLD_QUOTE_TOPIC', reply[1]['content'])

    def test_focus_preserves_full_snapshot_and_message_times(self):
        p = build_prompt(self.cfg, self.rows, '合成群', account=self.account)
        reply = build_reply_prompt(self.cfg, p, 2, reply_memory={})
        self.assertEqual(self.decoded(p)['messages'], self.decoded(reply)['messages'])
        self.assertEqual(self.decoded(reply)['messages'][1]['time'], NOW-1)
        for text in ('先前的话', 'ds这怎么看', '另外一个问题'):
            self.assertEqual(reply[1]['content'].count(text), 1)

    def test_budget_removes_whole_memory_lines_not_history_or_target(self):
        p = build_prompt(self.cfg, self.rows, '合成群', account=self.account)
        base = build_reply_prompt(self.cfg, p, 2, reply_memory={})
        self.cfg['target']['context_max_chars'] = len(base[1]['content'])
        reply = build_reply_prompt(self.cfg, p, 2, reply_memory={'2':'DO_NOT_TRUNCATE_THIS_MEMORY'})
        self.assertEqual(reply, base)
        self.cfg['target']['context_max_chars'] -= 1
        with self.assertRaises(ValueError):
            build_reply_prompt(self.cfg, p, 2, reply_memory={})

    def test_invalid_or_bot_target_cannot_start_a_body_request(self):
        p = build_prompt(self.cfg, self.rows, '合成群', account=self.account)
        for target in (1,999):
            with self.subTest(target=target), self.assertRaises(ProactiveError):
                build_reply_prompt(self.cfg, p, target)


class FrozenMemoryPipelineTests(unittest.TestCase):
    setUp = pipeline.PipelineTests.setUp
    make_bot = conversation.ConversationTests.make_bot
    add = pipeline.PipelineTests.add

    def test_main_process_freezes_target_memories_without_adding_them_to_judgment(self):
        self.add(1, sender='member_a', text='ds聊聊JOJO', mention=False)
        self.add(2, sender='member_b', text='ds另一个问题', mention=False)
        with self.bot.journal.db:
            self.bot.journal.set('cursor', 2)
        memory = Mock()
        memory.status.return_value = {}
        memory.prompt.side_effect = lambda labels, asker, *a, **kw: 'MEMORY_FOR_'+asker
        self.bot.memory = memory
        self.bot.proactive.step(self.now)
        job = self.bot.proactive.job[0]
        prompt, options = job.payload[1:]
        self.assertNotIn('MEMORY_FOR_', prompt[1]['content'])
        self.assertEqual(options['reply_memory'], {'1':'MEMORY_FOR_member_a','2':'MEMORY_FOR_member_b'})
        self.rows.append(msg(3, NOW, 'LATE_MESSAGE', 'member_b'))
        memory.prompt.side_effect = AssertionError('worker must not reread memory')
        reply = build_reply_prompt(self.cfg, prompt, 1, reply_memory=options['reply_memory'])
        self.assertNotIn('LATE_MESSAGE', reply[1]['content'])
        self.assertIn('MEMORY_FOR_member_a', reply[1]['content'])
        self.assertNotIn('MEMORY_FOR_member_b', reply[1]['content'])


if __name__ == '__main__':
    unittest.main()
