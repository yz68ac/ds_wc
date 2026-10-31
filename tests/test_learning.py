"""合成对话与可控时钟；验证真实来源、跨天晋级、后台队列和旧数据兼容。"""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch, Mock

from wxbridge.ai.memory import snapshot, extract
from wxbridge.config import DEFAULTS, load_config
from wxbridge.storage import expression_memory as expressions
from wxbridge.storage.person_memory import MemoryStore, DAY
from wxbridge.app.learning import enqueue_if_active, eligible_messages
from tests.support.memory import MemoryFixture, IDENTITY, suggestion, observation
from tests.support.pipeline import PipelineFixture
from tests.support.synthetic import NOW, msg


class LearningTests(unittest.TestCase):
    setUp = MemoryFixture.setUp

    def observe(self, when, *, who='wxid_a', scope='member', stance='support', basis=None,
                expression='[捂脸]', text=None, nickname='合成成员', replaces=None):
        identifier = self.memory.data['watermark']+1
        m = msg(identifier, int(when), text or '手机又拿反了'+expression, who)
        m['sender_name'] = nickname
        value = snapshot(self.cfg, [], [m], IDENTITY, self.memory, when)
        item = observation(scope, value['members'][who]['label'], expression, stance,
                           replaces, basis, 'm'+str(identifier))
        self.memory.apply(dict(updates=[], learning=[item]), value, when)
        return self.memory.data['learning']['records']

    def mature_group(self):
        for offset, who in ((0,'wxid_a'), (600,'wxid_b'), (1200,'wxid_c'), (DAY,'wxid_a'), (DAY+600,'wxid_b')):
            self.observe(NOW+offset, who=who, scope='group')
        return self.memory.data['learning']['records'][0]

    def test_member_requires_three_independent_scenes_across_two_dates(self):
        self.observe(NOW)
        self.observe(NOW+600)
        self.observe(NOW+1200)
        self.assertEqual(self.memory.data['learning']['records'][0]['state'], 'candidate')
        self.observe(NOW+DAY)
        self.assertEqual(self.memory.data['learning']['records'][0]['state'], 'stable')

    def test_same_conversation_and_repeat_window_never_promote(self):
        for i in range(5):
            self.observe(NOW+i)
        r = self.memory.data['learning']['records'][0]
        self.assertEqual(r['state'], 'candidate')
        before = copy.deepcopy(self.memory.data)
        m = msg(1, NOW, '[捂脸]', 'wxid_a')
        value = snapshot(self.cfg, [], [m], IDENTITY, self.memory, NOW+5)
        with self.assertRaises(ValueError):
            self.memory.apply(dict(updates=[],learning=[observation()]), value, NOW+5)
        self.assertEqual(self.memory.data, before)

    def test_group_requires_multiple_people_and_bot_needs_mature_basis(self):
        self.observe(NOW, scope='group')
        with self.assertRaisesRegex(ValueError, 'invalid_basis'):
            self.observe(NOW+600, scope='bot', basis=self.memory.data['learning']['records'][0]['id'])
        group = self.mature_group()
        self.assertEqual(group['state'], 'stable')
        self.observe(NOW+DAY+1200, scope='bot', basis=group['id'])
        self.assertTrue(any(r['scope'] == 'bot' and r['state'] == 'stable' for r in self.memory.data['learning']['records']))

    def test_one_person_cannot_create_group_norm_even_across_dates(self):
        for offset in (0,600,1200,DAY,DAY+600):
            self.observe(NOW+offset, scope='group')
        self.assertEqual(self.memory.data['learning']['records'][0]['state'], 'candidate')

    def test_explicit_preference_and_rejection_apply_immediately_without_affinity(self):
        self.observe(NOW, stance='explicit', text='我喜欢你用[捂脸]接我的糗事')
        self.assertEqual(self.memory.data['learning']['records'][0]['state'], 'stable')
        self.observe(NOW+1, stance='reject', text='别再对我用[捂脸]了')
        self.assertEqual(self.memory.data['learning']['records'][0]['state'], 'blocked')
        self.assertEqual(self.memory.data['members']['wxid_a']['affinity'], 50)

    def test_counterexample_demotes_and_same_token_other_scene_is_separate(self):
        for offset in (0,600,DAY):
            self.observe(NOW+offset)
        self.observe(NOW+DAY+600, stance='counter', text='这个[捂脸]不是在自嘲')
        self.assertEqual(self.memory.data['learning']['records'][0]['state'], 'candidate')
        m = msg(5, NOW+DAY+1200, '[捂脸]', 'wxid_a')
        value = snapshot(self.cfg, [], [m], IDENTITY, self.memory, m['time'])
        item = observation(source='m5')
        item['context'] = '被逗笑'
        self.memory.apply(dict(updates=[],learning=[item]),value,m['time'])
        self.assertEqual(len(self.memory.data['learning']['records']),2)

    def test_bot_own_text_old_messages_and_foreign_sources_cannot_be_evidence(self):
        own = msg(1,NOW-1,'[捂脸]','wxid_bot')
        human = msg(2,NOW,'这次有点好笑','wxid_a')
        value = snapshot(self.cfg,[own],[human],IDENTITY,self.memory,NOW)
        self.assertEqual(value['dialogue'][0]['member'], IDENTITY['account'])
        self.assertTrue(value['dialogue'][0]['is_bot'])
        for source in ('m1','m999'):
            with self.subTest(source=source),self.assertRaisesRegex(ValueError,'unknown_source'):
                self.memory.apply(dict(updates=[],learning=[observation(source=source)]),value,NOW)
        with self.assertRaisesRegex(ValueError,'not_observed'):
            self.memory.apply(dict(updates=[],learning=[observation(source='m2')]),value,NOW)
        self.assertEqual(self.memory.data['revision'],0)

    def test_explicit_feedback_can_reference_preceding_bot_but_not_silence(self):
        own = msg(1,NOW-1,'又摔了[捂脸]','wxid_bot')
        human = msg(2,NOW,'别用这个表情回应我','wxid_a')
        value = snapshot(self.cfg,[own],[human],IDENTITY,self.memory,NOW)
        self.memory.apply(dict(updates=[],learning=[observation(stance='reject',source='m2')]),value,NOW)
        self.assertEqual(self.memory.data['learning']['records'][0]['state'],'blocked')

    def test_initial_baseline_and_disabled_learning_preserve_existing_memories(self):
        self.memory.enable_learning(10,NOW)
        m = msg(1,NOW,'[捂脸]','wxid_a')
        value = snapshot(self.cfg,[],[m],IDENTITY,self.memory,NOW)
        self.assertFalse(value['learning_enabled'])
        with self.assertRaisesRegex(ValueError,'disabled'):
            self.memory.apply(dict(updates=[],learning=[observation()]),value,NOW)
        self.memory.apply(suggestion(),value,NOW)
        self.assertEqual(self.memory.data['members']['wxid_a']['affinity'],50)
        self.assertEqual(self.memory.data['learning']['records'],[])

    def test_expiration_and_prompt_budget_candidate_never_injected(self):
        self.observe(NOW)
        labels={'wxid_a':'U1'}
        self.assertNotIn('表达习惯',self.memory.prompt(labels,'wxid_a',NOW))
        self.assertTrue(self.memory.prune(NOW+7*DAY))
        self.assertEqual(self.memory.data['learning']['records'],[])
        self.observe(NOW+8*DAY,stance='explicit')
        text=self.memory.prompt(labels,'wxid_a',NOW+8*DAY,budget=300)
        self.assertIn('表达习惯',text)
        self.assertLessEqual(len(text),300)
        self.assertNotIn('表达习惯',self.memory.prompt(labels,'wxid_a',NOW+8*DAY,use_learning=False))
        self.assertNotIn('表达习惯',self.memory.prompt(labels,'wxid_a',NOW+99*DAY))

    def test_rename_reloads_same_owner_and_forget_removes_evidence_and_basis(self):
        group=self.mature_group()
        self.observe(NOW+DAY+1200,scope='bot',basis=group['id'])
        self.observe(NOW+DAY+1800,stance='explicit',nickname='新昵称')
        store=MemoryStore(self.temp.name,IDENTITY)
        self.assertEqual(store.data['members']['wxid_a']['nickname'],'新昵称')
        store.maintain('forget',member='wxid_a',watermark=20,now=NOW+DAY+2000)
        self.assertNotIn('wxid_a',store.data['members'])
        self.assertFalse(any(r['scope']=='bot' for r in store.data['learning']['records']))
        self.assertFalse(any(e['member']=='wxid_a' for r in store.data['learning']['records'] for e in r['evidence']))

    def test_group_and_bot_maintenance_do_not_clear_personal_facts(self):
        self.mature_group()
        self.memory.maintain('clear',scope='group',watermark=10,now=NOW+DAY+601)
        self.assertEqual(self.memory.data['learning']['records'],[])
        self.assertEqual(len(self.memory.data['members']),3)

    def test_invalid_batch_is_atomic_and_unknown_emoji_cannot_be_bot_preference(self):
        before=copy.deepcopy(self.memory.data)
        m=msg(1,NOW,'[捂脸]','wxid_a')
        value=snapshot(self.cfg,[],[m],IDENTITY,self.memory,NOW)
        with self.assertRaisesRegex(ValueError,'limit'):
            self.memory.apply(dict(updates=[],learning=[observation()]*4),value,NOW)
        self.assertEqual(self.memory.data,before)
        with self.assertRaisesRegex(ValueError,'invalid_basis'):
            self.observe(NOW,scope='bot',basis='unknown',expression='[未核实]')

    def test_old_file_optional_learning_fields_and_bad_new_file_rejected(self):
        self.memory.apply(suggestion(),snapshot(self.cfg,[],[msg(1,NOW,'hi','wxid_a')],IDENTITY,self.memory,NOW),NOW)
        self.assertNotIn('learning',MemoryStore(self.temp.name,IDENTITY).data)
        self.memory.enable_learning(1,NOW)
        self.observe(NOW+1,stance='explicit')
        value=json.loads(self.memory.path.read_text(encoding='utf-8'))
        value['learning']['records'][0]['state']='invented'
        self.memory.path.write_text(json.dumps(value),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'invalid_file'):
            MemoryStore(self.temp.name,IDENTITY)

    def test_prompt_file_edits_are_read_and_structured_protocol_stays_enforced(self):
        prompt=Path(self.temp.name)/'prompt.md'
        cfg=copy.deepcopy(self.cfg)
        cfg['memory']['system_prompt_file']=str(prompt)
        value=snapshot(cfg,[],[msg(1,NOW,'[捂脸]','wxid_a')],IDENTITY,self.memory,NOW)
        with patch('wxbridge.ai.client.client_for') as client:
            client.return_value.complete.return_value=dict(content='{"updates":[],"learning":[]}')
            for content in ('合成整理规则甲','合成整理规则乙'):
                prompt.write_text(content,encoding='utf-8')
                extract(cfg,value)
                system=client.return_value.complete.call_args.args[0][0]['content']
                self.assertIn(content,system)
                self.assertIn('learning每项严格为',system)
            prompt.write_text('',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'prompt_empty'):
                extract(cfg,value)

    def test_emoji_only_cannot_update_affinity_or_personality_even_if_model_suggests_it(self):
        value=snapshot(self.cfg,[],[msg(1,NOW,'[微笑]','wxid_a')],IDENTITY,self.memory,NOW)
        proposed=suggestion(delta=-3,kind='impression')
        self.memory.apply(proposed,value,NOW)
        self.assertEqual(self.memory.data['members']['wxid_a']['affinity'],50)
        self.assertEqual(self.memory.data['members']['wxid_a']['entries'],[])

    def test_disabling_and_reenabling_keeps_records_and_starts_new_evidence_baseline(self):
        self.observe(NOW,stance='explicit')
        self.memory.enable_learning(1,NOW,False)
        self.memory.enable_learning(20,NOW+DAY,True)
        self.assertEqual(self.memory.data['learning']['started_after_id'],20)
        self.assertEqual(len(self.memory.data['learning']['records']),1)

    def test_stable_caps_and_candidate_limit_are_deterministic(self):
        data=copy.deepcopy(self.memory.data)
        data['learning']=dict(started_after_id=0,records=[])
        for i in range(50):
            data['learning']['records'].append(dict(id=str(i),scope='member',owner='wxid_a',expression='词'+str(i),
                context='闲聊',meaning='轻松表达',state='candidate',confirmed_at=NOW+i,basis=None))
        expressions.prune(data,NOW+60)
        self.assertEqual(len(data['learning']['records']),40)
        for r in data['learning']['records']:
            r['state']='stable'
        expressions.prune(data,NOW+60)
        self.assertEqual(len(data['learning']['records']),5)

    def test_group_correction_invalidates_derived_bot_preference_and_expires_at_thirty_days(self):
        group=self.mature_group()
        self.observe(NOW+DAY+1200,scope='bot',basis=group['id'])
        self.memory.maintain('correct',scope='group',entry=group['id'],text='只表示轻松自嘲',
                             watermark=10,now=NOW+DAY+1800)
        self.assertFalse(any(r['scope']=='bot' for r in self.memory.data['learning']['records']))
        self.memory.prune(NOW+31*DAY+1800)
        self.assertEqual(self.memory.data['learning']['records'],[])


class LearningPipelineTests(unittest.TestCase):
    setUp=PipelineFixture.setUp
    make_bot=PipelineFixture.make_bot
    add=PipelineFixture.add
    restart=PipelineFixture.restart

    def enable(self):
        self.bot.close()
        self.cfg['memory'].update(enabled=True,directory=str(Path(self.temp.name)/'memory'))
        self.make_bot()

    def chat(self,count=10,one_sender=False,duplicate=False):
        for i in range(1,count+1):
            self.add(i,sender='wxid_a' if one_sender else 'wxid_'+str(i%3),
                     text='[捂脸]' if duplicate else '合成闲聊'+str(i),mention=False)

    def test_active_chat_enqueues_all_memory_without_send_and_survives_restart(self):
        self.enable()
        self.chat()
        self.bot.tick()
        self.assertEqual(len(self.jobs),1)
        self.assertEqual(self.jobs[0].kind,'memory')
        self.assertEqual(self.bot.memory.job[0]['origin'],'activity')
        self.assertEqual(len(self.bot.memory.job[1]['messages']),10)
        self.jobs[0].result=dict(ok=True,proposal=suggestion(label='wxid_1', source='m1'))
        self.bot.tick()
        self.assertEqual(self.bot.memory.store.data['members']['wxid_1']['affinity'],50)
        self.assertFalse(any(j.kind=='send' for j in self.jobs))
        self.restart()
        self.bot.tick()
        self.assertFalse(self.bot.journal.memory_jobs())

    def test_thresholds_reject_one_sender_duplicate_spam_and_old_baseline(self):
        for mode in ('one_sender','duplicate','baseline'):
            with self.subTest(mode=mode):
                # 独立队列重建由setUp管理，此处直接验证纯筛选结果和成员门槛。
                rows=[msg(i,NOW,'[捂脸]' if mode=='duplicate' else '闲聊'+str(i),
                          'wxid_a' if mode=='one_sender' else 'wxid_'+str(i%2)) for i in range(1,11)]
                settings=self.cfg['memory']['learning']
                rows=eligible_messages(rows,'wxid_bot',NOW,settings,10 if mode=='baseline' else 0)
                self.assertFalse(len(rows)>=10 and len({m['sender'] for m in rows})>=2)

    def test_reply_ids_do_not_collide_and_old_jobs_migrate_in_window_order(self):
        journal=self.bot.journal
        with journal.db:
            journal.enqueue_memory('activity','activity:10',[dict(local_id=10,time=NOW)], [NOW-300,NOW])
            journal.enqueue_memory('reply','reply:1',[dict(local_id=1,time=NOW-1)])
            journal.enqueue_memory('reply','reply:1',[dict(local_id=1,time=NOW-1)])
        jobs=journal.memory_jobs()
        self.assertEqual([j['origin'] for j in jobs],['reply','activity'])
        self.assertEqual(len({j['id'] for j in jobs}),2)
        self.restart()
        self.assertEqual(len(self.bot.journal.memory_jobs()),2)

    def test_background_failure_retry_is_throttled_and_foreground_continues(self):
        self.enable()
        self.chat()
        self.bot.tick()
        self.jobs[0].result=dict(ok=False,error='PRIVATE_MODEL_BODY')
        self.bot.tick()
        job=self.bot.journal.memory_jobs()[0]
        self.assertGreaterEqual(job['retry_at']-self.now,300)
        self.add(11)
        self.bot.tick()
        self.assertTrue(any(j.kind=='generate' for j in self.jobs))
        self.assertNotIn('PRIVATE_MODEL_BODY',self.bot.status_path.read_text())

    def test_recovered_passive_window_includes_bot_context_and_filters_newer_ids(self):
        self.enable()
        self.chat()
        self.bot.tick()
        self.add(11,sender='wxid_self',text='我是背景话语',mention=False)
        self.restart()
        self.bot.tick()
        _,value,_=self.bot.memory.job
        self.assertTrue(all(m['local_id']<=10 for m in value['messages']))
        self.assertEqual(value['through'],10)

    def test_foreground_two_requests_keep_running_passive_job_on_its_own_channel(self):
        self.enable()
        self.chat()
        self.bot.tick()
        worker=self.bot.memory.job[2]
        self.add(11,sender='wxid_a')
        self.bot.tick()
        self.add(12,sender='wxid_b')
        self.bot.tick()
        self.assertFalse(worker.closed)
        self.assertIsNotNone(self.bot.memory.job)
        self.assertEqual(len(self.bot.replies.jobs),2)
        self.assertEqual(self.bot.journal.memory_jobs()[0]['state'],'running')

    def test_passive_batches_wait_five_minutes_and_skip_processed_window(self):
        self.enable()
        self.chat()
        self.bot.tick()
        self.jobs[0].result=dict(ok=True,proposal=dict(updates=[]))
        self.bot.tick()
        self.now+=60
        for i in range(11,21):
            self.add(i,sender='wxid_'+str(i%2),text='新闲聊'+str(i),mention=False)
        self.bot.tick()
        self.assertEqual(len(self.jobs),1)
        self.now+=240
        self.bot.tick()
        self.assertEqual(len(self.jobs),2)
        self.assertEqual(min(m['local_id'] for m in self.bot.memory.job[1]['messages']),11)

    def test_enabling_passive_later_does_not_backfill_existing_chat(self):
        self.cfg['memory']['learning']['passive_enabled']=False
        self.enable()
        self.chat()
        self.bot.tick()
        self.assertEqual(self.jobs,[])
        self.cfg['memory']['learning']['passive_enabled']=True
        self.restart()
        self.add(11,text='启用后的新消息',mention=False)
        self.bot.tick()
        self.assertEqual(self.jobs,[])
        self.assertEqual(self.bot.journal.get('passive_learning')['started_after_id'],10)


def load_tests(loader, tests, pattern):
    return unittest.TestSuite([loader.loadTestsFromTestCase(LearningTests),loader.loadTestsFromTestCase(LearningPipelineTests)])
