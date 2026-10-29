"""Private chats batch directly into full replies; no real models or WeChat."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from model_support import default_config
import test_context_pipeline as pipeline
from wxbridge.ai.roles import validate_models


class PrivateReplyTests(unittest.TestCase):
    setUp = pipeline.PipelineTests.setUp
    add = pipeline.PipelineTests.add
    sends = pipeline.PipelineTests.sends
    generated = pipeline.PipelineTests.generated
    restart = pipeline.PipelineTests.restart

    def make_bot(self):
        self.cfg['target']['chat'] = 'wxid_friend'
        self.cfg['models']['participation']['model'] = ''
        self.store.resolve_chat.return_value = ('wxid_friend', '合成私聊')
        pipeline.PipelineTests.make_bot(self)

    def test_private_message_starts_full_reply_without_decision_or_mention(self):
        self.add(1, text='你好，聊聊今天的事吧', mention=False)
        with patch('wxbridge.app.participation.build_prompt', side_effect=AssertionError('no judgment')):
            self.bot.tick()
        self.assertEqual([job.kind for job in self.jobs], ['generate'])
        self.assertFalse(self.decision_jobs)
        self.assertEqual(self.bot.journal.head()['trigger_ids'], [1])
        self.assertIn('私聊：合成私聊', self.jobs[0].payload[1][1]['content'])
        self.assertFalse(self.bot.journal.participation_status()['decision_required'])
        self.assertIsNone(self.bot.models.lanes['background'].client)

    def test_private_burst_is_merged_once_before_generating(self):
        self.cfg['participation']['debounce_seconds'] = 2
        self.add(1, text='我今天遇到一个问题', mention=False)
        self.bot.tick()
        self.assertFalse(self.jobs)
        self.now += 1
        self.add(2, text='这是相关补充', mention=False)
        self.bot.tick()
        self.assertFalse(self.jobs)
        self.now += 2
        self.bot.tick()
        self.assertEqual([job.kind for job in self.jobs], ['generate'])
        self.assertEqual(self.bot.journal.head()['trigger_ids'], [1, 2])
        self.assertFalse(self.decision_jobs)

    def test_background_work_does_not_block_private_reply(self):
        channels = deepcopy(self.bot.models.status())
        channels['background']['running'] = True
        self.add(1, text='直接向你提问', mention=False)
        with patch.object(self.bot.models, 'status', return_value=channels):
            self.bot.tick()
        self.assertEqual([job.kind for job in self.jobs], ['generate'])
        self.assertFalse(self.decision_jobs)

    def test_full_reply_is_sent_and_confirmed_without_deciding_again(self):
        self.add(1, text='谢谢，今天聊得不错', mention=False)
        self.bot.tick()
        reply = '这是直接交给完整回复模型生成的内容。'*5
        self.generated(reply)
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.sends()[0].payload[2], reply)
        self.sends()[0].result = dict(ok=True, ui=dict(operation=dict(submitted=True)))
        self.add(2, sender='wxid_self', text=reply, mention=False)
        self.bot.tick()
        self.bot.tick()
        self.assertFalse(self.bot.journal.tasks())
        self.assertFalse(self.decision_jobs)
        self.assertEqual(len(self.sends()), 1)

    def test_restart_preserves_pending_burst_and_original_expiry(self):
        self.cfg['participation']['debounce_seconds'] = 2
        self.add(1, text='尚未完成合并', mention=False)
        self.bot.tick()
        expiry = self.bot.journal.participation_status()['recent'][0]['expires_at']
        self.restart()
        self.now += 2
        self.bot.tick()
        self.assertEqual([job.kind for job in self.jobs], ['generate'])
        self.assertEqual(self.bot.journal.participation_status()['recent'][0]['expires_at'], expiry)
        self.assertFalse(self.decision_jobs)

    def test_restart_resumes_approved_reply_without_new_decision(self):
        self.add(1, text='已经进入完整回复', mention=False)
        self.bot.tick()
        first = self.jobs[-1]
        self.restart()
        self.bot.tick()
        self.assertTrue(first.closed)
        self.assertEqual(len([job for job in self.jobs if job.kind == 'generate']), 2)
        self.assertEqual(self.bot.journal.head()['trigger_ids'], [1])
        self.assertFalse(self.decision_jobs)

    def test_private_reply_keeps_auto_reply_switch_and_sender_binding(self):
        self.add(1, sender='wxid_wrong', text='错误归属', mention=False)
        self.bot.tick()
        self.assertFalse(self.jobs)
        self.assertEqual(self.bot.journal.status()['recent_intake'][-1]['reason'], 'private_sender_mismatch')
        self.cfg['participation']['enabled'] = False
        self.add(2, text='已关闭自动回复', mention=False)
        self.bot.tick()
        self.assertFalse(self.jobs)
        self.assertFalse(self.decision_jobs)


class PrivateModelSettingsTests(unittest.TestCase):
    def test_private_chat_does_not_require_participation_model_id(self):
        cfg = default_config()
        cfg['target']['chat'] = 'wxid_friend'
        cfg['models']['participation']['model'] = ''
        before = deepcopy(cfg)
        validate_models(cfg)
        self.assertEqual(cfg, before)

    def test_groups_still_require_participation_model_even_with_wxid_prefix(self):
        cfg = default_config()
        cfg['models']['participation']['model'] = ''
        for chat in ('synthetic@chatroom', 'wxid_synthetic@chatroom'):
            cfg['target']['chat'] = chat
            with self.subTest(chat=chat), self.assertRaisesRegex(ValueError, 'missing_model:participation'):
                validate_models(cfg)
