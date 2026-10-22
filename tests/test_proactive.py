"""合成群聊、真实SQLite、可控时钟；不调用API或微信。"""
from __future__ import annotations

import copy
import json
import unittest
from unittest.mock import Mock, patch

from wxbridge.ai.proactive import activity, build_prompt, evaluate, parse_decision
from wxbridge.config import DEFAULTS, load_config
from wxbridge.app.workers import _work
from synthetic import NOW, msg
import test_context_pipeline as pipeline


class DecisionTests(unittest.TestCase):
    def test_strict_decisions_and_plain_short_text(self):
        self.assertEqual(parse_decision('{"speak":false,"text":""}'), dict(speak=False,text=''))
        self.assertEqual(parse_decision('{"speak":true,"text":"**这事有点意思。**"}'),dict(speak=True,text='这事有点意思'))
        for data in ({'speak':'false','text':''}, {'speak':True,'text':''},
                     {'speak':False,'text':'内部想法'}, {'speak':True,'text':'字'*31},
                     {'speak':True,'text':'短句','reason':'不允许的心理解释'}, []):
            with self.subTest(data=data), self.assertRaises(ValueError):
                parse_decision(json.dumps(data,ensure_ascii=False))

    def test_window_text_count_deduplicates_and_detects_own_image(self):
        rows=[msg(1,NOW-300,'边界'),msg(2,NOW-301,'旧消息'),msg(3,NOW+1,'未来消息'),
              msg(4,NOW,'图片'),msg(5,NOW,'',sender=''),msg(6,NOW,'本人发图','self')]
        rows[3]['type']=3; rows[-1]['type']=3
        texts,own=activity([*rows,rows[0]],'self',NOW,300)
        self.assertEqual([m['local_id'] for m in texts],[1])
        self.assertTrue(own)

    def test_prompt_is_not_a_fake_mention_and_preserves_personality_and_memory(self):
        cfg=copy.deepcopy(DEFAULTS)
        cfg['deepseek'].update(system_prompt_file='',system_prompt='合成人格')
        rows=[msg(i,NOW,'普通文字') for i in range(1,11)]
        prompt=build_prompt(cfg,rows,'合成群',dict(labels={'wxid_friend':'U1'},text='合成背景'))
        self.assertTrue(prompt[0]['content'].startswith('合成人格'))
        self.assertIn('保持沉默',prompt[0]['content'])
        self.assertNotIn('@你的人',prompt[1]['content'])
        self.assertIn('合成背景',prompt[1]['content'])
        self.assertEqual(prompt[1]['content'].count('普通文字'),10)
        cfg['target']['context_max_chars']=10
        with self.assertRaises(ValueError):
            build_prompt(cfg,rows,'合成群')

    def test_background_thinking_disabled_and_foreground_config_preserved(self):
        cfg=copy.deepcopy(DEFAULTS)
        client=Mock()
        client.complete.return_value={'content':'{"speak":false,"text":""}','reasoning_content':'PRIVATE_REASONING'}
        original=copy.deepcopy(cfg)
        with patch('wxbridge.ai.client.DeepSeekClient',return_value=client) as factory:
            self.assertFalse(evaluate(cfg,[])['speak'])
        request_cfg=factory.call_args.args[0]
        self.assertEqual(request_cfg['deepseek']['thinking'],'disabled')
        self.assertEqual(request_cfg['deepseek']['max_tokens'],256)
        self.assertEqual(cfg,original)
        self.assertIsNotNone(client.complete.call_args.kwargs['deadline'])

    def test_worker_returns_only_structured_decision(self):
        import logging
        prior=logging.root.manager.disable
        connection=Mock()
        try:
            with patch('wxbridge.ai.proactive.evaluate',return_value=dict(speak=False,text='')):
                _work(connection,'proactive',({},[]))
        finally:
            logging.disable(prior)
        self.assertEqual(connection.send.call_args.args[0],dict(ok=True,decision=dict(speak=False,text='')))

    def test_config_rejects_invalid_switch_and_limits(self):
        for values in ({'enabled':1},{'min_messages':True},{'min_messages':0},
                       {'max_reply_chars':31},{'timeout_seconds':30,'reply_ttl_seconds':10}):
            defaults=copy.deepcopy(DEFAULTS)
            defaults['proactive'].update(values)
            with patch('wxbridge.config.DEFAULTS',defaults),patch('wxbridge.config.load_dotenv'):
                with self.assertRaises(ValueError):
                    load_config('__missing_synthetic_config__.json')


class ProactivePipelineTests(unittest.TestCase):
    # 复用已有SQLite/时钟/进程替身夹具，不继承和重跑整套旧用例。
    setUp=pipeline.PipelineTests.setUp
    add=pipeline.PipelineTests.add
    sends=pipeline.PipelineTests.sends
    restart=pipeline.PipelineTests.restart

    def make_bot(self):
        self.cfg['proactive']['enabled']=True
        # 原十条门槛的兼容回归；新策略在独立夹具中启用。
        self.cfg['proactive']['opportunities']['enabled']=False
        self.cfg['proactive']['conversation']['enabled']=False
        pipeline.PipelineTests.make_bot(self)

    def chat(self,count=10,start=1):
        for i in range(start,start+count):
            self.add(i,text='合成闲聊'+str(i),mention=False)
        self.bot.tick()

    def decide(self,speak=True,text='这个想法还挺有趣。'):
        self.bot.proactive.job[0].result=dict(ok=True,decision=dict(speak=speak,text=text if speak else ''))
        self.bot.tick()

    def test_nine_does_not_evaluate_ten_can_evaluate_once(self):
        self.chat(9)
        self.assertFalse(self.jobs)
        self.now+=10
        self.chat(1,10)
        self.assertEqual([job.kind for job in self.jobs],['proactive'])
        self.bot.tick()
        self.assertEqual(len(self.jobs),1)

    def test_speaking_uses_existing_queue_and_exact_outbound_confirmation(self):
        self.chat()
        self.decide()
        self.assertEqual(len(self.sends()),1)
        task=self.bot.journal.head()
        self.assertEqual(task['trigger_kind'],'proactive')
        self.assertEqual(task['triggers'],[])
        self.add(11,sender='wxid_self',text=self.sends()[0].payload[2],mention=False)
        self.bot.tick()
        self.assertFalse(self.bot.journal.tasks())
        self.assertEqual(self.bot.journal.task(task['id'])['last_ack_id'],11)
        self.assertEqual(self.bot.proactive.state()['outcome'],'sent')
        self.assertFalse(self.bot.journal.memory_jobs())

    def test_silent_does_not_send_and_no_repeated_api_for_unchanged_window(self):
        self.chat()
        self.decide(False)
        self.assertFalse(self.sends())
        self.now+=61
        self.bot.tick()
        self.assertEqual(len(self.jobs),1)
        self.assertEqual(self.bot.proactive.state()['outcome'],'silent')

    def test_silent_reconsideration_needs_interval_and_three_new_texts(self):
        self.chat()
        self.decide(False)
        self.now+=61
        self.chat(2,11)
        self.assertEqual(len(self.jobs),1)
        self.now+=10
        self.chat(1,13)
        self.assertEqual(len(self.jobs),2)

    def test_previous_bot_reply_does_not_block_a_new_entry_evaluation(self):
        self.add(1,sender='wxid_self',text='自己发的消息',mention=False)
        self.chat(10,2)
        self.assertEqual([job.kind for job in self.jobs], ['proactive'])

    def test_stale_future_images_and_empty_messages_cannot_meet_threshold(self):
        for i in range(1,11):
            self.add(i,text='旧消息',sent=NOW-301,mention=False)
            self.add(i+10,text='未来消息',sent=NOW+1,mention=False)
            self.add(i+20,text='[图片]',mention=False)['type']=3
        self.bot.tick()
        self.assertFalse(self.jobs)

    def test_real_mention_preempts_background_and_late_result_is_ignored(self):
        self.chat()
        old=self.bot.proactive.job[0]
        old.result=dict(ok=True,decision=dict(speak=True,text='旧接话'))
        self.add(11)
        self.bot.tick()
        self.assertTrue(old.closed)
        self.assertIsNone(self.bot.proactive.job)
        self.assertEqual(self.bot.journal.head()['trigger_kind'],'mention')
        self.assertFalse(self.sends())

    def test_second_poll_mention_preempts_before_starting_foreground_workers(self):
        self.chat()
        old=self.bot.proactive.job[0]
        original=self.bot._poll_once
        calls=0
        def poll():
            nonlocal calls
            calls+=1
            if calls==2:
                self.add(11)
                self.add(12,sender='another_member')
            original()
        with patch.object(self.bot,'_poll_once',side_effect=poll):
            self.bot.tick()
        self.assertTrue(old.closed)
        self.assertEqual(len(self.bot.generators),2)
        self.assertIsNone(self.bot.proactive.job)

    def test_optional_ack_never_enqueues_memory_even_when_memory_flag_is_true(self):
        task=self.bot.journal.enqueue_proactive('可发送短句',10,self.now,self.now+60)
        self.bot.journal.intend(task['id'],10,self.now,10)
        self.bot.journal.acknowledge(task['id'],11,self.now,memory=True)
        self.assertFalse(self.bot.journal.memory_jobs())

    def test_new_mention_cancels_unsent_persisted_optional_reply(self):
        task=self.bot.journal.enqueue_proactive('还未提交的短句',10,self.now,self.now+60)
        self.add(1)
        self.bot.tick()
        self.assertEqual(self.bot.journal.task(task['id'])['cancelled'],'foreground_or_disabled')
        self.assertEqual(self.bot.journal.head()['trigger_kind'],'mention')

    def test_optional_read_error_does_not_stop_intake_or_enqueue_send(self):
        self.store.messages_in_window.side_effect=ValueError('PRIVATE_DB_ERROR')
        self.chat()
        self.assertEqual(self.bot.last_local_id,10)
        self.assertFalse(self.sends())
        self.assertEqual(self.bot.proactive.state()['error'],'ValueError')

    def test_own_message_during_generation_cancels_before_send(self):
        self.chat()
        self.add(11,sender='wxid_self',text='手机人工发言',mention=False)
        self.decide()
        self.assertFalse(self.sends())
        self.assertFalse(self.bot.journal.tasks())

    def test_context_moved_on_or_generation_expired_does_not_send(self):
        self.chat()
        for i in range(11,14):
            self.add(i,text='后续新话题',mention=False)
        self.decide()
        self.assertFalse(self.sends())
        self.assertFalse(self.bot.journal.tasks())

    def test_slow_optional_api_does_not_stop_intake_and_error_does_not_block(self):
        self.chat()
        for i in range(11,14):
            self.add(i,text='正在继续聊天',mention=False)
        self.bot.tick()
        self.assertEqual(self.bot.last_local_id,13)
        self.bot.proactive.job[0].result=dict(ok=False,error='SyntheticError')
        self.bot.tick()
        self.assertIsNone(self.bot.proactive.job)
        self.assertFalse(self.bot.journal.head())
        self.assertEqual(self.bot.proactive.state()['outcome'],'error')

    def test_expired_result_does_not_send(self):
        self.chat()
        self.now+=61
        self.decide()
        self.assertFalse(self.sends())

    def test_restart_does_not_repeat_evaluation_of_same_messages(self):
        self.chat()
        self.decide(False)
        self.restart()
        self.now+=61
        self.bot.tick()
        self.assertEqual(len(self.jobs),1)

    def test_saved_optional_reply_revalidates_after_restart(self):
        self.chat()
        worker,through,expires=self.bot.proactive.job
        self.bot.proactive.cancel('synthetic_stop')
        task=self.bot.journal.enqueue_proactive('持久短句',through,self.now,expires)
        self.restart()
        self.now+=61
        self.bot.tick()
        self.assertEqual(self.bot.journal.task(task['id'])['phase'],'done')
        self.assertFalse(self.sends())

    def test_restart_after_submit_only_confirms_and_never_resends(self):
        self.chat()
        self.decide()
        self.add(11,sender='wxid_self',text=self.sends()[0].payload[2],mention=False)
        self.restart()
        self.bot.tick()
        self.assertFalse(self.bot.journal.tasks())
        self.assertEqual(len(self.sends()),1)

    def test_uncertain_optional_send_stays_pinned_for_confirmation_ahead_of_mentions(self):
        self.chat()
        self.decide()
        optional=self.bot.journal.head()
        self.add(11)
        self.bot.tick()
        self.assertEqual(self.bot.journal.head()['id'],optional['id'])
        self.assertTrue(self.bot.journal.head()['intent'])
        self.assertEqual(len(self.sends()),1)

    def test_memory_concurrency_slot_is_respected(self):
        self.cfg['queue']['generation_concurrency']=1
        self.bot.memory_job=(Mock(),Mock(),Mock())
        try:
            self.chat()
            self.assertFalse(self.jobs)
        finally:
            self.bot.memory_job=None

    def test_log_and_status_do_not_expose_body_or_reasoning(self):
        for i in range(1,11):
            self.add(i,text='CHAT_PRIVATE_SECRET',mention=False)
        with self.assertLogs('wxbridge',level='INFO') as captured:
            self.bot.tick()
        exposed=' '.join(captured.output)+json.dumps(self.bot.journal.status())
        self.assertNotIn('CHAT_PRIVATE_SECRET',exposed)
        self.assertNotIn('PRIVATE_REASONING',exposed)


if __name__=='__main__':
    unittest.main()
