"""可控时钟验证成员活动续期、静默退出、重启和收发事务；全部数据为合成。"""
import copy
import unittest
from unittest.mock import patch

from wxbridge.config import DEFAULTS, load_config
import test_conversation as conversation


class ParticipationWindowTests(unittest.TestCase):
    setUp = conversation.ConversationTests.setUp
    make_bot = conversation.ConversationTests.make_bot
    add = conversation.ConversationTests.add
    sends = conversation.ConversationTests.sends
    restart = conversation.ConversationTests.restart
    ordinary = conversation.ConversationTests.ordinary
    decision = conversation.ConversationTests.decision
    confirm = conversation.ConversationTests.confirm
    enter = conversation.ConversationTests.enter

    def session(self):
        return self.bot.journal.get('proactive_session')

    def test_real_mention_opens_window_before_generation_or_send(self):
        row = self.add(1)
        self.bot.tick()
        self.assertEqual(self.session()['origin'], 'mention')
        self.assertEqual(self.session()['expires_at'], row['time']+120)
        self.assertFalse(self.sends())
        self.assertFalse(self.bot.journal.get('proactive_entries'))

    def test_slow_foreground_answer_does_not_restart_expired_window(self):
        self.add(1)
        self.bot.tick()
        worker = next(j for j in self.jobs if j.kind == 'generate')
        self.now += 121
        self.bot.tick()
        self.assertIsNone(self.session())
        worker.result = dict(ok=True, reply='迟到的正常回复仍应发送')
        self.bot.tick()
        self.assertEqual(len(self.sends()), 1)
        self.confirm()
        self.assertIsNone(self.session())
        self.assertEqual(self.bot.journal.status()['queue_length'], 0)

    def test_own_reply_never_extends_member_deadline(self):
        self.add(1)
        self.bot.tick()
        expires = self.session()['expires_at']
        self.now += 40
        next(j for j in self.jobs if j.kind == 'generate').result = dict(ok=True, reply='正常回复')
        self.bot.tick()
        self.now += 10
        self.confirm()
        self.assertEqual(self.session()['expires_at'], expires)
        self.now += 70
        self.bot.tick()
        self.assertIsNone(self.session())

    def test_member_messages_extend_window_during_slow_foreground_generation(self):
        self.add(1)
        self.bot.tick()
        self.now += 100
        self.ordinary('成员图片', sender='wxid_other', type=3)
        self.assertEqual(self.session()['expires_at'], self.now+120)
        self.now += 30
        next(j for j in self.jobs if j.kind == 'generate').result = dict(ok=True, reply='正常回复')
        self.bot.tick()
        self.confirm()
        self.assertEqual(self.session()['last_activity_at'], self.now-30)

    def test_unlimited_followups_only_member_messages_renew(self):
        self.enter()
        for count in range(1, 5):
            self.now += 20
            self.ordinary('换一位群友也能自然接话', sender='wxid_other')
            expires = self.session()['expires_at']
            self.decision()
            self.now += 5
            self.confirm()
            self.assertEqual(self.session()['followups'], count)
            self.assertEqual(self.session()['expires_at'], expires)
        self.assertEqual(len(self.bot.journal.get('proactive_entries')), 1)

    def test_media_renews_without_requesting_model_or_assuming_image_contents(self):
        self.enter()
        count = len(self.jobs)
        for kind in (3, 47, 49):
            self.now += 100
            row = self.add(max(m['local_id'] for m in self.rows)+1, sender='wxid_other', mention=False)
            row.update(type=kind, text='')
            self.bot.tick()
            self.assertEqual(self.session()['expires_at'], self.now+120)
            self.assertEqual(len(self.jobs), count)
        self.now += 120
        self.bot.tick()
        self.assertIsNone(self.session())
        self.assertEqual(len(self.jobs), count)

    def test_system_future_untrusted_old_and_own_messages_do_not_renew(self):
        self.enter()
        expires = self.session()['expires_at']
        self.now += 10
        cases = (dict(type=10000), dict(time=int(self.now+1)), dict(time=int(self.now-500)),
                 dict(time_source='observed'), dict(sender='wxid_self', type=3), dict(sender=''))
        for fields in cases:
            self.ordinary('不应续期', **fields)
            self.assertEqual(self.session()['expires_at'], expires)

    def test_addressing_other_members_renews_but_is_not_a_reply_target(self):
        self.enter()
        self.now += 100
        self.ordinary('别人的话题', mentioned_users=['wxid_other'])
        self.assertIsNone(self.bot.proactive.job)
        self.assertEqual(self.session()['expires_at'], self.now+120)
        self.now += 10
        self.ordinary('引用别人的内容', reference=dict(sender='wxid_other', kind='text'))
        self.assertIsNone(self.bot.proactive.job)
        self.assertEqual(self.session()['expires_at'], self.now+120)

    def test_silent_decisions_and_changed_topic_do_not_end_window(self):
        self.enter()
        for _ in range(3):
            self.now += 10
            self.ordinary('另一个话题', sender='wxid_other')
            self.assertEqual(self.bot.proactive.pending['mode'], 'followup')
            self.decision(speak=False)
            self.assertIsNotNone(self.session())
        self.assertEqual(len(self.sends()), 1)
        self.now += 120
        self.bot.tick()
        self.assertIsNone(self.session())

    def test_expired_window_cannot_be_revived_by_later_uninvited_message(self):
        self.enter()
        self.now += 121
        self.ordinary('两分钟以后的图片', sender='wxid_other', type=3)
        self.assertIsNone(self.session())
        self.assertIsNone(self.bot.proactive.job)
        self.assertEqual(len(self.sends()), 1)

    def test_delayed_read_uses_message_time_and_duplicate_restart_cannot_renew(self):
        self.enter()
        self.now += 100
        self.ordinary('迟读的图片', type=3, time=int(self.now-50))
        expires = self.now+70
        self.assertEqual(self.session()['expires_at'], expires)
        self.restart()
        self.now += 69
        self.bot.tick()
        self.assertEqual(self.session()['expires_at'], expires)
        self.now += 1
        self.bot.tick()
        self.assertIsNone(self.session())

    def test_restart_replays_only_continuous_member_activity_chain(self):
        self.enter()
        start = self.now
        self.add(3, sent=int(start+100), mention=False)['type'] = 3
        self.add(4, sent=int(start+200), mention=False)['type'] = 47
        self.now += 250
        self.restart()
        self.bot.tick()
        self.assertEqual(self.session()['expires_at'], start+320)
        self.assertEqual(self.bot.last_local_id, 4)
        count = len(self.jobs)
        self.bot.tick()
        self.assertEqual(len(self.jobs), count)

    def test_restart_does_not_bridge_a_two_minute_quiet_gap(self):
        self.enter()
        self.add(3, sent=int(self.now+150), mention=False)['type'] = 3
        self.now += 200
        self.restart()
        self.bot.tick()
        self.assertIsNone(self.session())

    def test_zero_hourly_limit_allows_new_entry_after_normal_cooldown(self):
        with self.bot.journal.db:
            self.bot.journal.set('proactive_entries', [self.now-400-i*50 for i in range(5)])
        self.ordinary('ds来聊一句')
        self.assertEqual(self.bot.proactive.pending['mode'], 'entry')

    def test_new_mention_overrides_entry_cooldown_and_retired_backoff(self):
        self.enter()
        with self.bot.journal.db:
            self.bot.journal.set('proactive_entry_not_before', self.now+600)
        self.now += 10
        self.add(3, sender='wxid_other')
        self.bot.tick()
        self.assertEqual(self.session()['origin'], 'mention')
        self.assertEqual(self.session()['participant'], 'wxid_other')

    def test_late_entry_confirmation_cannot_replace_newer_mention_window(self):
        self.ordinary('ds来聊一句')
        self.decision()
        self.now += 10
        row = self.add(2, sender='wxid_other')
        self.bot.tick()
        session_id = self.session()['session_id']
        expires = row['time']+120
        self.confirm()
        self.assertEqual(self.session()['session_id'], session_id)
        self.assertEqual(self.session()['expires_at'], expires)
        self.assertEqual(self.session()['origin'], 'mention')

    def test_activity_and_cursor_roll_back_together_on_observation_error(self):
        self.add(1)
        self.add(2, mention=False)
        original = self.bot.proactive.observe
        def broken(message, accepted, now):
            original(message, accepted, now)
            if message['local_id'] == 2:
                raise RuntimeError('synthetic_failure')
        with patch.object(self.bot.proactive, 'observe', side_effect=broken):
            with self.assertRaises(RuntimeError):
                self.bot._poll_once()
        self.assertEqual(self.bot.last_local_id, 0)
        self.assertFalse(self.bot.journal.tasks())
        self.assertIsNone(self.session())
        self.bot.tick()
        self.assertEqual(self.bot.last_local_id, 2)
        self.assertIsNotNone(self.session())

    def test_old_timer_policy_is_retired_without_touching_queue_or_cursor(self):
        self.add(1)
        self.bot.tick()
        with self.bot.journal.db:
            self.bot.journal.set('proactive_session', dict(anchor_id=1, participant='wxid_friend',
                expires_at=self.now+120, followups=0, origin='reply'))
        self.restart()
        self.assertIsNone(self.session())
        self.assertEqual(self.bot.last_local_id, 1)
        self.assertEqual(len(self.bot.journal.tasks()), 1)


class WindowConfigurationTests(unittest.TestCase):
    def test_zero_limits_are_valid_but_negative_and_boolean_limits_are_rejected(self):
        for value in (0, 1, 2, -1, True):
            defaults = copy.deepcopy(DEFAULTS)
            defaults['proactive']['max_entries_per_hour'] = value
            defaults['proactive']['conversation']['max_followups'] = value
            with self.subTest(value=value), patch('wxbridge.config.DEFAULTS', defaults), patch('wxbridge.config.load_dotenv'):
                if type(value) is int and value >= 0:
                    cfg = load_config('__missing_synthetic_config__.json')
                    self.assertEqual(cfg['proactive']['conversation']['max_followups'], value)
                else:
                    with self.assertRaises(ValueError):
                        load_config('__missing_synthetic_config__.json')
