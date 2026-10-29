"""Unified decisions, full replies and confirmation with synthetic inputs only."""
import json
import unittest
import test_context_pipeline as pipeline


class ConversationTests(unittest.TestCase):
    setUp = pipeline.PipelineTests.setUp
    add = pipeline.PipelineTests.add
    sends = pipeline.PipelineTests.sends
    restart = pipeline.PipelineTests.restart
    generated = pipeline.PipelineTests.generated

    def make_bot(self):
        self.cfg['participation'].update(enabled=True, debounce_seconds=0)
        pipeline.PipelineTests.make_bot(self)
        def make(kind, payload, **kwargs):
            job = pipeline.FakeJob(kind, payload, **kwargs)
            (self.decision_jobs if kind == 'participation' else self.jobs).append(job)
            return job
        self.bot.job_factory = make

    def ordinary(self, text, sender='wxid_friend', **fields):
        identifier = max((m['local_id'] for m in self.rows), default=0)+1
        row = self.add(identifier, sender=sender, text=text, mention=False)
        row.update(fields)
        self.bot.tick()
        return row

    def decision(self, reply_to=None, *, speak=True, text=None, target=None):
        if reply_to is None:
            reply_to = [target or self.bot.participation.pending['source_ids'][-1]] if speak else []
        self.bot.participation.job.result = dict(ok=True, decision=dict(reply_to=reply_to))
        self.bot.tick()
        if text is not None and reply_to:
            self.generated(text)

    def confirm(self):
        worker = self.sends()[-1]
        worker.result = dict(ok=True, ui=dict(operation=dict(submitted=True)))
        return self.ordinary(worker.payload[2], sender='wxid_self')

    def enter(self):
        row = self.ordinary('ds，过来接个话')
        self.decision([row['local_id']], text='自然接话')
        self.confirm()

    def answer_mention(self, sender='wxid_friend'):
        identifier = max((m['local_id'] for m in self.rows), default=0)+1
        self.add(identifier, sender=sender)
        self.bot.tick()
        self.decision([identifier], text='正常回复真实提问')
        self.confirm()

    def test_single_ordinary_message_waits_for_model_approval(self):
        row = self.ordinary('今天聊聊编程')
        self.assertEqual(self.bot.participation.pending['source_ids'], [row['local_id']])
        self.assertFalse(self.jobs)
        self.assertFalse(self.bot.journal.tasks())
        self.decision([row['local_id']])
        self.assertEqual(self.bot.journal.head()['trigger_kind'], 'participation')
        self.assertEqual(self.bot.journal.head()['triggers'], [row])
        self.assertEqual(self.jobs[-1].kind, 'generate')

    def test_real_mention_does_not_force_a_reply(self):
        self.add(1)
        self.bot.tick()
        self.assertFalse(self.jobs)
        self.decision([])
        self.assertFalse(self.bot.journal.tasks())
        self.assertEqual(self.bot.journal.participation_status()['counts']['silent'], 1)

    def test_mentions_of_other_people_are_context_not_an_admission_gate(self):
        row = self.ordinary('你们刚说的那个问题', mentioned_users=['wxid_other'])
        data = json.loads(self.bot.participation.job.payload[1][1]['content'])
        self.assertEqual(data['source_ids'], [row['local_id']])
        self.assertEqual(data['messages'][-1]['mentions'], ['wxid_other'])
        self.decision([])
        self.assertFalse(self.jobs)

    def test_batch_selects_multiple_people_without_merging_their_questions(self):
        for identifier, sender in ((1, 'wxid_a'), (2, 'wxid_b'), (3, 'wxid_a')):
            self.add(identifier, sender=sender, mention=False)
        self.bot.tick()
        self.decision([1, 2, 3])
        tasks = self.bot.journal.tasks()
        self.assertEqual([t['trigger_ids'] for t in tasks], [[1, 3], [2]])
        self.assertEqual([t['sender'] for t in tasks], ['wxid_a', 'wxid_b'])
        self.assertEqual(len(self.bot.generators), 2)

    def test_unselected_sources_are_recorded_as_silent(self):
        self.add(1, mention=False)
        self.add(2, sender='wxid_other', mention=False)
        self.bot.tick()
        self.decision([2])
        self.assertEqual(self.bot.journal.head()['trigger_ids'], [2])
        counts = self.bot.journal.participation_status()['counts']
        self.assertEqual((counts['silent'], counts['approved']), (1, 1))

    def test_new_arrivals_do_not_cancel_reserved_work(self):
        first = self.ordinary('第一个问题')
        worker = self.bot.participation.job
        second = self.ordinary('第二个人的问题', sender='wxid_other')
        self.assertIs(self.bot.participation.job, worker)
        self.assertEqual(self.bot.participation.pending['source_ids'], [first['local_id']])
        worker.result = dict(ok=True, decision=dict(reply_to=[first['local_id']]))
        self.bot.tick()
        self.assertEqual(self.bot.journal.head()['trigger_ids'], [first['local_id']])
        self.assertEqual(self.bot.participation.pending['source_ids'], [second['local_id']])
        self.assertTrue(worker.closed)

    def test_uninvited_quote_uses_full_image_pipeline_after_approval(self):
        reference = dict(kind='image', sender='wxid_other', available=False, text='', image_md5='a'*32)
        self.store.image_binding.return_value = dict(error='local_image_not_cached', candidates=[])
        row = self.ordinary('这张图挺有意思', reference=reference)
        self.store.image_binding.assert_not_called()
        self.decision([row['local_id']])
        self.store.image_binding.assert_called_once_with('synthetic@chatroom', reference)
        self.assertEqual(len(self.jobs[-1].payload), 4)
        self.assertEqual(self.jobs[-1].payload[3]['triggers'][0]['reference'], reference)

    def test_silence_is_not_repeated_after_poll_or_restart(self):
        self.ordinary('路过')
        self.decision([])
        self.now += 10
        self.bot.tick()
        self.restart()
        self.bot.tick()
        self.assertEqual(len(self.decision_jobs), 1)
        self.assertFalse(self.jobs)

    def test_restart_restores_pending_decision_with_a_new_token(self):
        row = self.ordinary('还没判断')
        old = self.bot.participation.job
        token = self.bot.participation.pending['token']
        self.restart()
        self.bot.tick()
        self.assertTrue(old.closed)
        self.assertNotEqual(self.bot.participation.pending['token'], token)
        self.assertEqual(self.bot.participation.pending['source_ids'], [row['local_id']])
        old.result = dict(ok=True, decision=dict(reply_to=[row['local_id']]))
        self.assertFalse(self.bot.journal.tasks())

    def test_expired_approval_never_generates_or_sends(self):
        row = self.ordinary('会过期的问题')
        worker = self.bot.participation.job
        self.now += self.cfg['target']['max_message_age_seconds']+1
        worker.result = dict(ok=True, decision=dict(reply_to=[row['local_id']]))
        self.bot.tick()
        self.assertFalse(self.jobs)
        self.assertEqual(self.bot.journal.participation_status()['counts']['expired'], 1)

    def test_invalid_decision_has_only_one_retry(self):
        self.ordinary('不许选择旧ID')
        self.decision([9999])
        self.assertFalse(self.jobs)
        self.now += 2
        self.bot.tick()
        self.assertEqual(len(self.decision_jobs), 2)
        self.decision([9999])
        self.now += 3
        self.bot.tick()
        self.assertEqual(len(self.decision_jobs), 2)
        self.assertEqual(self.bot.journal.participation_status()['counts']['error'], 1)

    def test_full_reply_can_exceed_thirty_chars_and_is_confirmed_once(self):
        row = self.ordinary('这个问题需要详细解释')
        answer = '这是完整回复流程生成的解释。'*8
        self.decision([row['local_id']], text=answer)
        self.assertEqual(self.sends()[0].payload[2], answer)
        self.confirm()
        self.bot.tick()
        self.assertFalse(self.bot.journal.tasks())
        self.assertEqual(len(self.sends()), 1)

    def test_disabling_cancels_unsubmitted_automatic_work(self):
        self.ordinary('尚未决定')
        worker = self.bot.participation.job
        self.cfg['participation']['enabled'] = False
        self.bot.tick()
        self.assertTrue(worker.closed)
        self.assertEqual(self.bot.journal.participation_status()['counts']['cancelled'], 1)
        self.assertFalse(self.jobs)

    def test_logs_and_status_exclude_body_and_raw_error(self):
        self.ordinary('CHAT_PRIVATE_SECRET')
        with self.assertLogs('wxbridge', level='WARNING') as captured:
            self.bot.participation.job.result = dict(ok=False, error='RAW_PRIVATE_SECRET')
            self.bot.tick()
        exposed = json.dumps(self.bot.journal.status())+' '.join(captured.output)
        self.assertNotIn('CHAT_PRIVATE_SECRET', exposed)
        self.assertNotIn('RAW_PRIVATE_SECRET', exposed)

    def test_approved_reply_deadline_prevents_late_result_or_send(self):
        row = self.ordinary('等太久就不要回了')
        self.decision([row['local_id']])
        worker = self.jobs[-1]
        deadline = self.bot.journal.head()['reply_expires_at']
        self.now = deadline+1
        worker.result = dict(ok=True, reply='LATE_REPLY_SECRET')
        self.bot.tick()
        self.assertTrue(worker.closed)
        self.assertFalse(self.bot.journal.tasks())
        self.assertFalse(self.sends())

    def test_expiry_does_not_discard_existing_send_intent(self):
        row = self.ordinary('已经提交的回复先确认')
        self.decision([row['local_id']], text='已提交的完整回复')
        task = self.bot.journal.head()
        self.assertIsNotNone(task['intent'])
        self.now = task['reply_expires_at']+1
        self.bot.tick()
        self.assertIsNotNone(self.bot.journal.head()['intent'])
        self.assertEqual(len(self.sends()), 1)

    def test_budget_shrinks_batch_without_consuming_extra_model_attempts(self):
        self.add(1, sender='wxid_a', text='甲'*300, mention=False)
        self.add(2, sender='wxid_b', text='乙'*300, mention=False)
        self.cfg['target']['context_max_chars'] = 650
        self.bot.tick()
        self.assertEqual(len(self.decision_jobs), 1)
        self.assertEqual(len(self.bot.participation.pending['source_ids']), 1)
        status = self.bot.journal.participation_status()
        self.assertEqual(status['running'], 1)
        self.assertEqual(status['pending'], 1)
        self.assertTrue(all(r['attempts'] <= 1 for r in status['recent']))
