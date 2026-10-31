"""Every approved source uses the complete reply pipeline and private diagnostics."""
import json
import unittest

import tests.support.conversation as conversation


class ParticipationRoutingTests(unittest.TestCase):
    setUp = conversation.ConversationFixture.setUp
    make_bot = conversation.ConversationFixture.make_bot
    add = conversation.ConversationFixture.add
    sends = conversation.ConversationFixture.sends
    restart = conversation.ConversationFixture.restart
    ordinary = conversation.ConversationFixture.ordinary

    def approve(self, row):
        self.bot.participation.job.result = dict(ok=True, decision=dict(reply_to=[row['local_id']]))
        self.bot.tick()
        return next(job for job in self.jobs if job.kind == 'generate' and not job.closed)

    def test_image_reference_defers_availability_to_full_pipeline_and_binds_in_owner(self):
        ref = dict(kind='image', available=False, text='', sender='wxid_other', image_md5='a'*32)
        self.store.image_binding.return_value = dict(error='local_image_not_cached', candidates=[])
        row = self.ordinary('这张图怎么看', reference=ref, message_kind='quoted_text', raw_type=244813135921)
        worker = self.bot.participation.job
        _, prompt, options = worker.payload
        data = json.loads(prompt[1]['content'])
        self.assertEqual(data['source_ids'], [row['local_id']])
        self.assertNotIn('opportunity', data)
        self.assertFalse(data['messages'][0]['reference']['available'])
        self.assertEqual(options, dict(source_ids=[row['local_id']]))
        self.store.image_binding.assert_not_called()
        generated = self.approve(row)
        task = self.bot.journal.head()
        self.assertEqual(task['trigger_kind'], 'participation')
        self.assertEqual(task['triggers'][0]['reference'], ref)
        self.store.image_binding.assert_called_once_with('synthetic@chatroom', ref)
        self.assertEqual(len(generated.payload[3]['images']), 1)
        self.assertEqual(self.bot.journal.participation_status()['counts'].get('approved'), 1)

    def test_ordinary_message_has_full_reply_options_without_old_short_answer_limit(self):
        row = self.ordinary('今天又有个好笑的事')
        generated = self.approve(row)
        text = '完整回复可以自然展开说明。'*8
        generated.result = dict(ok=True, reply=text)
        self.bot.tick()
        self.assertEqual(self.sends()[0].payload[2], text)
        self.assertNotIn('最多30字', generated.payload[1][0]['content'])
        self.assertEqual(self.bot.journal.head()['trigger_ids'], [row['local_id']])

    def test_silent_verdict_survives_new_polling_and_restart(self):
        self.ordinary('这句话你怎么看')
        self.bot.participation.job.result = dict(ok=True, decision=dict(reply_to=[]))
        self.bot.tick()
        verdict = self.bot.journal.participation_status()['last_decision']
        self.assertEqual(verdict['reply_to'], [])
        self.now += 10
        self.ordinary('路过一下')
        self.assertEqual(self.bot.journal.participation_status()['last_decision'], verdict)
        self.restart()
        self.assertEqual(self.bot.journal.participation_status()['last_decision'], verdict)
        self.assertEqual(self.bot.journal.participation_status()['counts'], dict(silent=1))

    def test_diagnostic_history_is_bounded_and_never_contains_source_or_reasoning(self):
        for index in range(25):
            self.ordinary('SOURCE_SECRET_'+str(index))
            self.bot.participation.job.result = dict(ok=True, decision=dict(reply_to=[]),
                reasoning_content='THOUGHT_SECRET', text='REPLY_SECRET')
            self.bot.tick()
        state = self.bot.journal.participation_status()
        self.assertEqual(len(state['recent']), 20)
        self.assertEqual(state['counts'], dict(silent=25))
        for secret in ('SOURCE_SECRET', 'REPLY_SECRET', 'THOUGHT_SECRET'):
            self.assertNotIn(secret, json.dumps(state))


if __name__ == '__main__':
    unittest.main()
