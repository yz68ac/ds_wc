"""Invited image routing and safe judgment history, with synthetic inputs only."""
import copy
import json
import unittest

import test_conversation as conversation


class ParticipationRoutingTests(unittest.TestCase):
    setUp = conversation.ConversationTests.setUp
    make_bot = conversation.ConversationTests.make_bot
    add = conversation.ConversationTests.add
    sends = conversation.ConversationTests.sends
    restart = conversation.ConversationTests.restart
    ordinary = conversation.ConversationTests.ordinary
    decision = conversation.ConversationTests.decision

    def test_named_image_defers_availability_to_full_pipeline_and_binds_in_main(self):
        ref = dict(kind='image', available=False, text='', sender='wxid_other', image_md5='a'*32)
        self.store.image_binding.return_value = dict(error='local_image_not_cached', candidates=[])
        row = self.ordinary('ds 这张图怎么看', reference=ref, message_kind='quoted_text', raw_type=244813135921)
        worker = self.bot.proactive.job[0]
        _, prompt, options = worker.payload
        data = json.loads(prompt[1]['content'])
        self.assertEqual(data['opportunity']['response_path'], 'full_reply')
        self.assertFalse(data['messages'][0]['reference']['available'])
        self.assertTrue(options['judgment_only'])
        self.store.image_binding.assert_not_called()
        worker.result = dict(ok=True, decision=dict(speak=True, reply_to=row['local_id'], text='', handoff=True))
        self.bot.tick(); self.bot.tick()
        task = self.bot.journal.head()
        self.assertEqual(task['trigger_kind'], 'addressed')
        self.assertEqual(task['triggers'][0]['reference'], ref)
        self.store.image_binding.assert_called_once_with('synthetic@chatroom', ref)
        generated = next(j for j in self.jobs if j.kind == 'generate')
        self.assertEqual(len(generated.payload[3]['images']), 1)
        self.assertEqual(self.bot.proactive.state()['last_decision']['outcome'], 'handed_to_reply')

    def test_routing_metadata_does_not_mutate_opportunity_and_uninvited_stays_short(self):
        from wxbridge.ai.proactive import build_prompt
        from synthetic import msg, NOW
        candidate = dict(mode='entry', reason='activity', source_ids=[1])
        before = copy.deepcopy(candidate)
        prompt = build_prompt(self.cfg, [msg(1, NOW, '今天又有个好笑的事')], '合成群', opportunity=candidate)
        self.assertEqual(candidate, before)
        self.assertEqual(json.loads(prompt[1]['content'])['opportunity']['response_path'], 'short_reply')

    def test_silent_verdict_survives_new_message_polling_and_process_restart(self):
        self.ordinary('ds这句话你怎么看')
        self.decision(speak=False)
        verdict = self.bot.proactive.state()['last_decision']
        self.assertEqual(verdict['opportunity'], 'addressed')
        self.assertFalse(verdict['speak'])
        self.now += 10
        self.ordinary('路过一下')
        self.assertEqual(self.bot.proactive.state()['last_decision'], verdict)
        self.restart()
        self.assertEqual(self.bot.proactive.state()['last_decision'], verdict)
        self.assertEqual(self.bot.proactive.state()['decision_counts'], dict(silent=1))

    def test_verdict_history_is_bounded_and_never_contains_reply_or_source_body(self):
        pending = dict(mode='followup', reason='conversation', source_ids=[1], text='SOURCE_SECRET')
        decision = dict(speak=True, reply_to=1, text='REPLY_SECRET', reasoning_content='THOUGHT_SECRET')
        for i in range(25):
            self.bot.proactive.remember_decision(pending, decision, 'queued', self.now+i)
        state = self.bot.proactive.state()
        self.assertEqual(len(state['decision_history']), 20)
        self.assertEqual(state['decision_history'][0]['at'], self.now+5)
        self.assertEqual(state['decision_counts'], dict(speak=25))
        for secret in ('SOURCE_SECRET', 'REPLY_SECRET', 'THOUGHT_SECRET'):
            self.assertNotIn(secret, json.dumps(state))
