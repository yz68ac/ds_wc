"""Synthetic regressions for topic drift, selected recipients and quoted identity."""
from tests.support.model_support import default_config
from contextlib import nullcontext
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from tests.support.synthetic import NOW, msg
import tests.support.memory as memory_tests
from tests.support.memory import IDENTITY, suggestion
import tests.support.pipeline as pipeline
import tests.support.conversation as conversation
from wxbridge.ai.memory import snapshot
from wxbridge.ai.identity import member_labels, reference_data
from wxbridge.ai.prompts import build_prompt as normal_prompt
from wxbridge.ai.participation import build_prompt, evaluate, ParticipationError
from wxbridge.config import DEFAULTS
from wxbridge.storage.memory_selection import memory_dialogue, MemoryTopic


class RetrievalTests(unittest.TestCase):
    setUp = memory_tests.MemoryFixture.setUp

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

    def test_shared_emoticon_cannot_make_an_old_game_a_topic_match(self):
        self.seed(1, '提出开个Warframe账号，并跟[Sly]表情招呼一起玩', 'scene')
        self.seed(2, '吐槽前两部剧情，觉得JOJO第三部更好')
        text = self.prompt('JOJO前两部怎么样[Sly][Sly]')
        self.assertIn('JOJO', text)
        self.assertNotIn('Warframe', text)

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
        self.cfg = default_config()

        self.cfg['system_prompt_file'] = str(Path(__file__).resolve().parents[1]/'prompts/system.md')
        self.account = 'wxid_synthetic_bot'
        self.rows = [{**msg(1, NOW-2, '先前的话', self.account), 'sender_name': '同名'},
                     {**msg(2, NOW-1, 'ds这怎么看', 'person_a'), 'sender_name': '同名'},
                     {**msg(3, NOW, '另外一个问题', 'person_b'), 'sender_name': '同名'}]
        self.rows[1]['reference'] = dict(kind='text', available=True, sender=self.account,
            sender_name='同名', text='原话', reference=dict(kind='text', available=True,
                sender='person_b', sender_name='同名', text='更早的原话'))

    def decoded(self, prompt):
        return json.JSONDecoder().raw_decode(prompt[1]['content'])[0]

    def test_normal_and_participation_nested_quotes_share_identity_without_nickname_guessing(self):
        labels = member_labels(self.rows, [self.rows[1]], self.account)
        saved = copy.deepcopy(self.rows)
        p = build_prompt(self.cfg, self.rows, '合成群', dict(labels=labels), source_ids=[2,3], account=self.account)
        quote = self.decoded(p)['messages'][1]['reference']
        self.assertEqual(quote['sender'], self.account)
        self.assertEqual(quote['reference']['sender'], labels['person_b'])
        self.assertEqual(self.decoded(p)['members'][self.account], dict(alias='同名'))
        n = normal_prompt(self.cfg, self.rows[:1], self.rows[1], '合成群',
                          memory=dict(labels=labels), account=self.account)
        quote = self.decoded(n)['messages'][-1]['reference']
        self.assertEqual(quote['sender'], self.account)
        self.assertEqual(quote['reference']['sender'], labels['person_b'])
        self.assertNotIn('"member": "BOT"', json.dumps([p,n], ensure_ascii=False))
        self.assertEqual(self.rows, saved)

    def test_unknown_quote_author_is_not_bot_based_on_display_name(self):
        ref = reference_data(dict(kind='text', sender_name='BOT（你）', text='冒名昵称'), {}, self.account)
        self.assertEqual(ref['member'], '')
        self.assertFalse(ref['is_bot'])

    def test_selected_message_gets_its_own_memory_instead_of_last_speaker(self):
        p = build_prompt(self.cfg, self.rows, '合成群', source_ids=[2,3], account=self.account)
        client = Mock()
        client.complete.return_value = dict(content='{"reply_to":[2]}')
        with patch('wxbridge.ai.client.client_for', return_value=client):
            result = evaluate(self.cfg, p, source_ids=[2,3])
        self.assertEqual(client.complete.call_count, 1)
        memories = {'person_a':'SELECTED_MEMBER_MEMORY', 'person_b':'OTHER_MEMBER_MEMORY'}
        target = next(row for row in self.rows if row['local_id'] == result['reply_to'][0])
        reply = normal_prompt(self.cfg, self.rows, target, '合成群',
            memory=dict(text=memories[target['sender']]), account=self.account)
        self.assertNotIn('MEMBER_MEMORY', p[1]['content'])
        self.assertIn('SELECTED_MEMBER_MEMORY', reply[1]['content'])
        self.assertNotIn('OTHER_MEMBER_MEMORY', reply[1]['content'])
        self.assertEqual(self.decoded(reply)['source_ids'], [2])
        self.assertEqual(self.decoded(reply)['messages'][-1]['sender'], 'person_a')
        self.assertIn('原话', reply[1]['content'])
        self.assertEqual(result['reply_to'], [2])

    def test_same_member_distinct_quotes_bind_memory_to_message_id(self):
        self.rows[2]['sender'] = 'person_a'
        memories = {'2':'OLD_QUOTE_TOPIC', '3':'NEW_QUOTE_TOPIC'}
        reply = normal_prompt(self.cfg, self.rows, self.rows[2], '合成群',
            memory=dict(text=memories[str(self.rows[2]['local_id'])]), account=self.account)
        self.assertIn('NEW_QUOTE_TOPIC', reply[1]['content'])
        self.assertNotIn('OLD_QUOTE_TOPIC', reply[1]['content'])

    def test_decision_preserves_times_and_full_reply_uses_only_history_before_selected_source(self):
        p = build_prompt(self.cfg, self.rows, '合成群', source_ids=[2,3], account=self.account)
        self.assertEqual([m['time'] for m in self.decoded(p)['messages']], [NOW-2,NOW-1,NOW])
        reply = normal_prompt(self.cfg, self.rows, self.rows[1], '合成群', account=self.account)
        for text in ('先前的话', 'ds这怎么看'):
            self.assertEqual(reply[1]['content'].count(text), 1)
        self.assertNotIn('另外一个问题', reply[1]['content'])

    def test_budget_removes_whole_memory_lines_not_history_or_target(self):
        self.cfg['target']['context_budget_strategy'] = 'strict'
        base = normal_prompt(self.cfg, self.rows, self.rows[1], '合成群', account=self.account)
        self.cfg['target']['context_max_chars'] = len(base[1]['content'])
        reply = normal_prompt(self.cfg, self.rows, self.rows[1], '合成群',
            memory=dict(text='DO_NOT_TRUNCATE_THIS_MEMORY'), account=self.account)
        self.assertEqual(reply, base)
        self.cfg['target']['context_max_chars'] -= 1
        with self.assertRaises(ValueError):
            normal_prompt(self.cfg, self.rows, self.rows[1], '合成群', account=self.account)

    def test_invalid_or_bot_target_cannot_enter_the_decision_scope(self):
        for target in (1,999):
            with self.subTest(target=target), self.assertRaises(ParticipationError):
                build_prompt(self.cfg, self.rows, '合成群', source_ids=[target], account=self.account)


class FrozenMemoryPipelineTests(unittest.TestCase):
    setUp = pipeline.PipelineFixture.setUp
    make_bot = conversation.ConversationFixture.make_bot
    add = pipeline.PipelineFixture.add

    def test_owner_reads_selected_memory_after_decision_and_freezes_generation_request(self):
        self.add(1, sender='member_a', text='聊聊JOJO', mention=False)
        self.add(2, sender='member_b', text='另一个问题', mention=False)
        memory = Mock()
        memory.data = dict(watermark=0, learning=dict(started_after_id=0), processed_ids=[])
        memory.status.return_value = {}
        self.cfg['memory']['learning']['passive_enabled'] = False
        view = memory.view.return_value
        view.revision = 7
        view.prompt.side_effect = lambda labels, asker, *a, **kw: 'MEMORY_FOR_'+asker
        view.reply_relationship.return_value = None
        self.bot.memory.store = memory
        self.cfg['memory']['enabled'] = True
        self.bot.tick()
        job = self.bot.participation.job
        prompt, options = job.payload[1:]
        self.assertNotIn('MEMORY_FOR_', prompt[1]['content'])
        memory.view.assert_not_called()
        job.result = dict(ok=True, decision=dict(reply_to=[1]))
        self.bot.tick()
        memory.view.assert_called_once()
        view.prompt.assert_called_once()
        self.assertEqual(view.prompt.call_args.args[1], 'member_a')
        generated = next(job for job in self.jobs if job.kind == 'generate')
        frozen = copy.deepcopy(generated.payload[1])
        self.rows.append(msg(3, NOW, 'LATE_MESSAGE', 'member_b'))
        memory.view.side_effect = AssertionError('worker must not reread memory')
        view.prompt.side_effect = AssertionError('worker must not reread memory')
        reply = generated.payload[1]
        self.assertEqual(reply, frozen)
        self.assertNotIn('LATE_MESSAGE', reply[1]['content'])
        self.assertIn('MEMORY_FOR_member_a', reply[1]['content'])
        self.assertNotIn('MEMORY_FOR_member_b', reply[1]['content'])


if __name__ == '__main__':
    unittest.main()
