"""真实记忆文件与可控时间：迁移、多维关系、去重、修正和情绪；不调用API。"""
import copy
import json
import unittest
from unittest.mock import patch

from wxbridge.ai.memory import extract, snapshot
from wxbridge.storage.person_memory import MemoryStore, DAY, affinity_value
from wxbridge.storage import relationships as relation
import test_memory as memory_tests
from synthetic import NOW, msg

IDENTITY = memory_tests.IDENTITY


def change(delta=0, source='m1', event='一次具体互动', strength='clear', revises=None):
    return dict(delta=delta, sources=[source] if delta else [], reason=event if delta else '',
                event=event if delta else '', strength=strength, revises=revises)


def proposal(source='m1', delta=0, trust=0, event='一次具体互动', strength='clear', mood=None, revises=None):
    return dict(updates=[dict(member='U1', memories=[], affinity=change(delta, source, event, strength, revises),
                             relationship=dict(trust=change(trust, source, event, strength), mood=mood))])


def emotion(source='m1', state='upset', intensity=2):
    return dict(state=state, intensity=intensity, reason='刚才的调侃令人有些无奈', sources=[source])


class RelationshipTests(unittest.TestCase):
    setUp = memory_tests.MemoryTests.setUp

    def turn(self, when=NOW, *, text=None, who='wxid_a', name='合成群友', delta=0, trust=0,
             event=None, strength='clear', mood=None, revises=None, update=True):
        identifier = self.memory.data['watermark']+1
        m = msg(identifier, when, text or f'第{identifier}次具体交流', who)
        m['sender_name'] = name
        value = snapshot(self.cfg, [m], [m], IDENTITY, self.memory, when)
        suggestion = proposal('m'+str(identifier), delta, trust, event or f'不同事件{identifier}',
                              strength, mood, revises) if update else dict(updates=[])
        self.memory.apply(suggestion, value, when)
        return self.memory.data['members'][who]

    def test_old_file_keeps_score_aliases_facts_and_watermark_without_guessing_familiarity(self):
        p = self.turn(delta=3)
        p['affinity'] = 77
        p['aliases'] = ['曾用名']
        p.pop('relationship')
        for item in p['affinity_evidence']:
            for key in ('id', 'event', 'strength', 'revises', 'fingerprints'):
                item.pop(key, None)
        self.memory.persist(NOW)
        before = self.memory.path.read_bytes()
        loaded = MemoryStore(self.temp.name, IDENTITY, create=False)
        self.assertEqual(loaded.path.read_bytes(), before)
        self.assertEqual(relation.state(loaded.data['members']['wxid_a'])['familiarity'], 0)
        self.assertTrue(loaded.prune(NOW))
        person = MemoryStore(self.temp.name, IDENTITY).data['members']['wxid_a']
        self.assertEqual((person['affinity'], person['aliases']), (77, ['曾用名']))
        self.assertEqual((person['relationship']['trust'], person['relationship']['familiarity']), (50, 0))
        self.assertIsNone(person['relationship']['mood'])
        self.assertEqual(loaded.data['watermark'], 1)
        self.assertIn('id', person['affinity_evidence'][0])
        self.assertFalse(loaded.prune(NOW))

    def test_familiarity_accumulates_different_scenes_across_days_without_model_updates(self):
        self.assertEqual(self.turn(update=False)['relationship']['familiarity'], 2)
        self.assertEqual(self.turn(NOW+600, update=False)['relationship']['familiarity'], 2)
        self.assertEqual(self.turn(NOW+1800, update=False)['relationship']['familiarity'], 3)
        self.assertEqual(self.turn(NOW+3600, update=False)['relationship']['familiarity'], 3)
        p = self.turn(NOW+DAY, update=False)
        self.assertEqual(p['relationship']['familiarity'], 5)
        self.assertEqual((p['affinity'], p['relationship']['trust']), (50, 50))

    def test_repeated_text_and_emoticons_do_not_farm_familiarity_or_relationship(self):
        p = self.turn(text='谢谢你', delta=1, trust=1)
        self.assertEqual(p['relationship']['familiarity'], 2)
        p = self.turn(NOW+DAY, text='谢谢你[偷笑]', delta=1, trust=1)
        self.assertEqual((p['affinity'], p['relationship']['trust'], p['relationship']['familiarity']), (51, 51, 2))
        for i, text in enumerate(('[微笑]', '🙂', '？？？'), 2):
            p = self.turn(NOW+i*DAY, text=text, delta=3, trust=1, mood=emotion('m'+str(i+1)))
            self.assertEqual((p['affinity'], p['relationship']['trust'], p['relationship']['familiarity']), (51, 51, 2))
            self.assertIsNone(p['relationship']['mood'])

    def test_ten_minute_scene_gate_and_same_event_deduplicate_after_restart(self):
        self.turn(delta=3, trust=1, event='帮助排查一次连接问题')
        p = self.turn(NOW+30, delta=3, trust=1)
        self.assertEqual((p['affinity'], p['relationship']['trust']), (53, 51))
        self.memory = MemoryStore(self.temp.name, IDENTITY)
        p = self.turn(NOW+3600, delta=3, trust=1, event='帮助排查一次连接问题')
        self.assertEqual((p['affinity'], p['relationship']['trust']), (53, 51))
        p = self.turn(NOW+3601, delta=1, trust=1)
        self.assertEqual((p['affinity'], p['relationship']['trust']), (54, 52))

    def test_weak_evidence_only_moves_affinity_one_and_does_not_move_trust(self):
        p = self.turn(delta=3, trust=1, strength='weak')
        self.assertEqual((p['affinity'], p['relationship']['trust']), (51, 50))

    def test_trust_daily_absolute_cap_and_no_automatic_inactivity_penalty(self):
        for i, delta in enumerate((1, -1, 1, 1), 1):
            p = self.turn(NOW+i*601, trust=delta)
        self.assertEqual(p['relationship']['trust'], 51)
        self.assertEqual(sum(p['relationship']['trust_daily'].values()), 3)
        self.memory.prune(NOW+100*DAY)
        p = self.memory.data['members']['wxid_a']
        self.assertEqual(p['relationship']['trust'], 51)
        self.assertEqual(p['relationship']['familiarity'], 3)

    def test_mood_is_short_lived_weakens_and_does_not_change_long_term_scores(self):
        p = self.turn(mood=emotion())
        self.assertEqual((p['affinity'], p['relationship']['trust']), (50, 50))
        self.assertIn('短时有点不悦', relation.describe(p, 50, NOW))
        self.assertIn('短时略微有点不悦', relation.describe(p, 50, NOW+relation.MOOD_TTL/2))
        self.assertNotIn('不悦', relation.describe(p, 50, NOW+relation.MOOD_TTL))
        self.assertTrue(self.memory.prune(NOW+relation.MOOD_TTL))
        self.assertIsNone(self.memory.data['members']['wxid_a']['relationship']['mood'])
        self.assertFalse(self.memory.prune(NOW+relation.MOOD_TTL))

    def test_mood_neutral_can_clear_early_and_other_member_is_isolated(self):
        self.turn(mood=emotion())
        p = self.turn(NOW+1, who='wxid_b')
        self.assertIsNone(p['relationship']['mood'])
        self.assertIsNotNone(self.memory.data['members']['wxid_a']['relationship']['mood'])
        p = self.turn(NOW+2, mood=emotion('m3', state='neutral'))
        self.assertIsNone(p['relationship']['mood'])

    def test_mood_uses_source_time_not_processing_time(self):
        m = msg(1, NOW, '这只是玩笑', 'wxid_a')
        value = snapshot(self.cfg, [], [m], IDENTITY, self.memory, NOW)
        self.memory.apply(proposal(mood=emotion()), value, NOW+relation.MOOD_TTL+1)
        self.assertIsNone(self.memory.data['members']['wxid_a']['relationship']['mood'])

    def test_new_evidence_can_revise_an_old_score_once_despite_scene_gate(self):
        p = self.turn(delta=-3, event='误会了刚才的调侃')
        original = p['affinity_evidence'][0]['id']
        p = self.turn(NOW+30, delta=3, event='本人明确澄清是玩笑', revises=original)
        self.assertEqual(p['affinity'], 50)
        self.assertEqual(p['affinity_evidence'][-1]['revises'], original)
        before = copy.deepcopy(self.memory.data)
        with self.assertRaisesRegex(ValueError, 'unknown_revision'):
            self.turn(NOW+31, delta=3, revises=original)
        self.assertEqual(self.memory.data, before)

    def test_unknown_cross_member_or_old_source_cannot_change_relationship(self):
        self.turn(delta=1)
        m = msg(2, NOW+1, '新发言', 'wxid_b')
        value = snapshot(self.cfg, [], [m], IDENTITY, self.memory, NOW+1)
        for item in (proposal(trust=1), proposal(source='m999', mood=emotion('m999')),
                     proposal(source='m2', delta=1, revises='invalid')):
            before = copy.deepcopy(self.memory.data)
            with self.assertRaises(ValueError):
                self.memory.apply(item, value, NOW+1)
            self.assertEqual(self.memory.data, before)

    def test_invalid_new_fields_are_atomic_and_do_not_advance_watermark(self):
        m = msg(1, NOW, '明确实际发言', 'wxid_a')
        value = snapshot(self.cfg, [], [m], IDENTITY, self.memory, NOW)
        invalid = [dict(trust=2), dict(mood=emotion(state='angry')), dict(mood=emotion(intensity=True))]
        for kwargs in invalid:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.memory.apply(proposal(delta=3, **kwargs), value, NOW)
            self.assertEqual(self.memory.data['watermark'], 0)
            self.assertFalse(self.memory.path.exists())
        item = proposal(delta=3)
        item['updates'][0]['relationship']['familiarity'] = 100
        with self.assertRaises(ValueError):
            self.memory.apply(item, value, NOW)

    def test_revision_cannot_increase_original_effect_or_reverse_more_than_it(self):
        p = self.turn(delta=1)
        original = p['affinity_evidence'][0]['id']
        for delta, strength in ((1, 'clear'), (-3, 'clear'), (-1, 'weak')):
            before = copy.deepcopy(self.memory.data)
            with self.subTest(delta=delta, strength=strength), self.assertRaises(ValueError):
                self.turn(NOW+1, delta=delta, strength=strength, revises=original)
            self.assertEqual(before, self.memory.data)

    def test_clamping_and_familiarity_capacity_never_exceed_one_hundred(self):
        p = self.turn()
        p['affinity'] = 99
        p['relationship']['trust'] = 100
        p['relationship']['familiarity'] = 99
        p = self.turn(NOW+DAY, delta=3, trust=1)
        self.assertEqual((p['affinity'], p['relationship']['trust'], p['relationship']['familiarity']), (100, 100, 100))
        self.assertEqual(p['affinity_evidence'][-1]['delta'], 1)
        self.assertFalse(p['relationship']['trust_evidence'])

    def test_ten_bands_and_prompt_keep_scores_and_evidence_private(self):
        p = self.turn(mood=emotion(state='amused'))
        for score, name in ((9,'明显疏远'),(10,'保持距离'),(20,'有所保留'),(30,'平淡相处'),
                            (40,'普通相处'),(50,'相处轻松'),(60,'有些好感'),(70,'更加在意'),
                            (80,'亲近偏爱'),(90,'很亲近'),(100,'很亲近')):
            self.assertIn(name, relation.describe(p, score, NOW))
        p['affinity'] = 94
        p['relationship']['trust'] = 28
        prompt = self.memory.prompt({'wxid_a':'U1'}, 'wxid_a', NOW)
        for expected in ('初识', '很亲近', '谨慎信任', '尚不熟时保留分寸', '具体偏好和边界优先', '被逗乐'):
            self.assertIn(expected, prompt)
        self.assertNotIn('94', prompt)
        self.assertNotIn('28', prompt)
        self.assertNotIn('affinity_evidence', prompt)
        self.assertLessEqual(len(prompt), 1200)
        self.assertEqual(self.memory.prompt({'wxid_a':'U1'}, 'wxid_a', NOW, budget=10), '')

    def test_rename_does_not_reset_dimensions_and_forget_relationship_keeps_facts_watermark(self):
        self.turn(delta=1, trust=1, mood=emotion())
        p = self.turn(NOW+1, name='改了昵称')
        self.assertEqual((p['affinity'], p['relationship']['trust'], p['relationship']['familiarity']), (51, 51, 2))
        self.memory.maintain('forget', member='wxid_a', entry='relationship', watermark=2, now=NOW+2)
        p = MemoryStore(self.temp.name, IDENTITY).data['members']['wxid_a']
        self.assertEqual((p['nickname'], p['affinity'], p['relationship']['trust'], p['relationship']['familiarity']),
                         ('改了昵称', 50, 50, 0))
        self.assertIsNone(p['relationship']['mood'])
        self.assertEqual(self.memory.data['watermark'], 2)

    def test_decay_is_settled_once_when_a_zero_delta_update_arrives(self):
        self.turn(delta=3)
        p = self.turn(NOW+21*DAY)
        self.assertEqual(p['affinity'], 52)
        self.assertEqual(affinity_value(p, NOW+21*DAY), 52)
        self.assertEqual(affinity_value(p, NOW+42*DAY), 51)

    def test_disk_failure_keeps_all_dimensions_retryable(self):
        with patch('wxbridge.storage.person_memory.atomic_write', side_effect=OSError('PRIVATE')):
            with self.assertRaises(OSError):
                self.turn(delta=1, trust=1, mood=emotion())
        self.assertEqual(self.memory.data['watermark'], 0)
        self.assertEqual(self.memory.data['members'], {})
        p = self.turn(delta=1, trust=1, mood=emotion())
        self.assertEqual((p['affinity'], p['relationship']['trust'], p['relationship']['familiarity']), (51, 51, 2))

    def test_malformed_stored_relationship_is_rejected(self):
        self.turn()
        data = copy.deepcopy(self.memory.data)
        data['members']['wxid_a']['relationship']['trust'] = 101
        self.memory.path.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaises(ValueError):
            MemoryStore(self.temp.name, IDENTITY)

    def test_extractor_reuses_one_text_request_and_exposes_only_direct_evidence(self):
        self.turn(delta=1, trust=1)
        m = msg(2, NOW+1, '接着聊', 'wxid_a')
        value = snapshot(self.cfg, [], [m], IDENTITY, self.memory, NOW+1)
        with patch('wxbridge.ai.client.client_for') as client:
            client.return_value.complete.return_value = dict(content='{"updates":[]}')
            extract(self.cfg, value)
        self.assertEqual(client.return_value.complete.call_count, 1)
        prompt = client.return_value.complete.call_args.args[0]
        self.assertIn('不因不同意见', prompt[0]['content'])
        public = json.loads(prompt[1]['content'])
        self.assertEqual(public['existing']['U1']['relationship']['trust'], 51)
        self.assertEqual(public['messages'][0]['id'], 'm2')
        self.assertEqual(public['source_ids_by_member'], {'U1':['m2']})
        self.assertNotIn('avatars', public)

    def test_empty_historical_member_placeholder_is_ignored_without_reapplying_decay(self):
        self.turn(delta=1)
        old = msg(1, NOW, '旧发言', 'wxid_a')
        new = msg(2, NOW+1, '新发言', 'wxid_b')
        value = snapshot(self.cfg, [old], [new], IDENTITY, self.memory, NOW+1)
        item = proposal()['updates'][0]
        item['member'] = value['members']['wxid_a']['label']
        before = copy.deepcopy(self.memory.data['members']['wxid_a'])
        self.memory.apply(dict(updates=[item]), value, NOW+1)
        self.assertEqual(self.memory.data['members']['wxid_a'], before)
        self.assertEqual(self.memory.data['members']['wxid_b']['relationship']['familiarity'], 2)
        self.assertEqual(self.memory.data['watermark'], 2)

    def test_extractor_only_lists_new_speakers_as_update_members(self):
        self.turn()
        old = msg(1, NOW, '旧发言', 'wxid_a')
        new = msg(2, NOW+1, '新发言', 'wxid_b')
        value = snapshot(self.cfg, [old], [new], IDENTITY, self.memory, NOW+1)
        with patch('wxbridge.ai.client.client_for') as client:
            client.return_value.complete.return_value = dict(content='{"updates":[]}')
            extract(self.cfg, value)
        public = json.loads(client.return_value.complete.call_args.args[0][1]['content'])
        self.assertEqual(set(public['members']), {value['members']['wxid_b']['label']})
        self.assertEqual(public['source_ids_by_member'], {'U1':['m2']})

    def test_recovery_request_explicitly_disables_expression_learning_without_extra_call(self):
        m = msg(1, NOW, '实际发言', 'wxid_a')
        value = snapshot(self.cfg, [], [m], IDENTITY, self.memory, NOW)
        value.update(learning_enabled=False, learning_source_ids=[], learning_existing=[],
                     recovery=dict(section='expression_learning', reason='memory_learning_expression_not_observed'))
        with patch('wxbridge.ai.client.client_for') as client:
            client.return_value.complete.return_value = dict(content='{"updates":[],"learning":[]}')
            extract(self.cfg, value)
        self.assertEqual(client.return_value.complete.call_count, 1)
        prompt = client.return_value.complete.call_args.args[0]
        self.assertIn('必须返回learning=[]', prompt[0]['content'])
        self.assertFalse(json.loads(prompt[1]['content'])['learning_enabled'])


class RelationshipPipelineTests(unittest.TestCase):
    setUp = memory_tests.PipelineTests.setUp
    make_bot = memory_tests.PipelineTests.make_bot
    add = memory_tests.PipelineTests.add
    sends = memory_tests.PipelineTests.sends
    generated = memory_tests.PipelineTests.generated
    enable = memory_tests.MemoryPipelineTests.enable
    confirmed = memory_tests.MemoryPipelineTests.confirmed

    def test_repeated_learning_validation_failure_retries_narrowed_batch_then_next_batch_learns(self):
        import test_learning as learning_tests
        self.enable()
        self.confirmed()
        invalid = dict(updates=[], learning=[learning_tests.observation()])
        for attempt in range(1, 4):
            self.bot.memory_job[2].result = dict(ok=True, proposal=invalid)
            self.bot.tick()
            record = self.bot.journal.memory_jobs()[0]
            self.assertEqual(record['attempts'], attempt)
            self.assertEqual(record['last_error'], 'memory_learning_expression_not_observed')
            self.assertEqual(self.bot.memory.data['watermark'], 0)
            if attempt < 3:
                self.now = record['retry_at']
                self.bot.tick()
                self.assertIsNotNone(self.bot.memory_job)
        self.bot.tick()  # 新能力范围的请求可立即启动，未篡改原失败建议。
        record, value, job = self.bot.memory_job
        self.assertTrue(record['learning_suppressed'])
        self.assertFalse(value['learning_enabled'])
        self.assertEqual(value['learning_existing'], [])
        self.assertTrue(self.cfg['memory']['learning']['enabled'])
        job.result = dict(ok=True, proposal=proposal(delta=1, trust=1))
        self.bot.tick()
        self.assertFalse(self.bot.journal.memory_jobs())
        self.assertEqual(self.bot.journal.get('memory_error'), '')
        self.assertEqual(self.bot.journal.get('memory_recovery')['stage'], 'completed')
        self.assertEqual(self.bot.memory.data['members']['wxid_friend']['relationship']['trust'], 51)
        self.add(3, mention=False, text='下一批的新话题')
        self.bot.tick()
        with self.bot.journal.db:
            self.bot.journal.enqueue_memory('reply', 'synthetic-next', [dict(local_id=3,time=int(self.now))])
        self.now += 60
        self.bot.tick()
        self.assertTrue(self.bot.memory_job[1]['learning_enabled'])
        self.assertFalse(self.bot.memory_job[0].get('learning_suppressed'))

    def test_output_limit_is_reported_and_requeued_without_writing_partial_memory(self):
        self.enable()
        self.confirmed()
        revision = self.bot.memory.data['revision']
        self.bot.memory_job[2].result = dict(ok=False, error='output_limit')
        self.bot.tick()
        self.assertEqual(self.bot.journal.get('memory_error'), 'memory_output_limit')
        self.assertEqual(self.bot.memory.data['revision'], revision)
        self.assertEqual(self.bot.journal.memory_jobs()[0]['state'], 'pending')
        self.assertEqual(self.bot.journal.memory_jobs()[0]['attempts'], 1)
        self.add(3)
        self.bot.tick()
        self.assertTrue(self.bot.generators)

    def test_confirmed_reply_updates_relationship_and_next_reply_gets_natural_background(self):
        self.enable()
        self.confirmed()
        job = self.bot.memory_job[2]
        job.result = dict(ok=True, proposal=proposal(delta=1, trust=1, mood=emotion()))
        self.bot.tick()
        p = self.bot.memory.data['members']['wxid_friend']
        self.assertEqual((p['affinity'], p['relationship']['trust']), (51, 51))
        self.add(3)
        self.bot.tick()
        prompt = next(j for j in reversed(self.jobs) if j.kind == 'generate').payload[1]
        content = prompt[1]['content']
        self.assertIn('初识', content)
        self.assertIn('短时有点不悦', content)
        self.assertIn('不要公开分数', content)
        self.assertNotIn('affinity_evidence', content)
        self.assertEqual(len([j for j in self.jobs if j.kind == 'memory']), 1)
