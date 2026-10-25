"""Strong, target-bound relationship instructions without moving scores or extra API calls."""
import copy
import json
import unittest
from unittest.mock import Mock, patch

import test_memory as memory_tests
import test_context_pipeline as pipeline
import test_media as media
from synthetic import NOW, msg
from wxbridge.ai.prompts import build_prompt
from wxbridge.ai.proactive import build_prompt as participation_prompt, evaluate
from wxbridge.storage import relationships as relation


def state(score, **fields):
    return dict(affinity=score, familiarity=50, trust=50, **fields)


class RelationshipStyleTests(unittest.TestCase):
    setUp = memory_tests.MemoryTests.setUp
    snap = memory_tests.MemoryTests.snap

    def test_six_profiles_share_thresholds_and_have_distinct_behavior(self):
        expected=((0,'明显疏远'),(19,'明显疏远'),(20,'有些距离'),(39,'有些距离'),
                  (40,'普通相处'),(59,'普通相处'),(60,'有好感'),(74,'有好感'),
                  (75,'熟悉亲近'),(89,'熟悉亲近'),(90,'很亲近'),(100,'很亲近'))
        for score,band in expected:
            self.assertEqual(relation.affinity_profile(score)[1],band)
            self.assertIn('当前档位：'+band,relation.with_reply_policy('',state(score)))
        self.assertEqual(len({p[2] for p in relation.AFFINITY_PROFILES}),6)
        self.assertEqual(len({p[3] for p in relation.AFFINITY_PROFILES}),6)

    def test_relationship_survives_empty_memory_budget_without_changing_history(self):
        trigger=msg(1,NOW,'你今天怎么样','wxid_a')
        base=build_prompt(self.cfg,[],trigger,'合成群')
        self.cfg['target']['context_max_chars']=len(base[1]['content'])
        low=build_prompt(self.cfg,[],trigger,'合成群',memory=dict(text='',relationship_for='wxid_a',relationship=state(10)))
        high=build_prompt(self.cfg,[],trigger,'合成群',memory=dict(text='',relationship_for='wxid_a',relationship=state(95)))
        self.assertEqual(low[1],base[1]);self.assertEqual(high[1],base[1])
        self.assertIn('不主动接梗、追问或关心',low[0]['content'])
        self.assertIn('允许流露亲近或依恋',high[0]['content'])
        self.assertNotIn('affinity',high[0]['content'])
        self.assertNotIn('wxid_a',high[0]['content'])

    def test_rebuild_replaces_old_policy_once_and_wrong_sender_cannot_reuse_it(self):
        a=msg(1,NOW,'合成问题','wxid_a');b=msg(2,NOW,'另一个问题','wxid_b')
        memory=dict(relationship_for='wxid_a',relationship=state(95))
        first=build_prompt(self.cfg,[],a,'合成群',memory=memory)
        repeated=build_prompt(self.cfg,[],a,'合成群',memory=memory,system_text=first[0]['content'])
        self.assertEqual(first[0],repeated[0])
        self.assertEqual(repeated[0]['content'].count(relation.REPLY_POLICY_START),1)
        wrong=build_prompt(self.cfg,[],b,'合成群',memory=memory,system_text=first[0]['content'])
        self.assertNotIn(relation.REPLY_POLICY_START,wrong[0]['content'])

    def test_untrusted_text_and_invalid_numbers_never_become_system_instructions(self):
        for invalid in (dict(affinity=True,familiarity=0,trust=50),state(101),state(-1),dict(affinity='95',familiarity=0,trust=50)):
            self.assertEqual(relation.with_reply_policy('人格',invalid),'人格')
        text=relation.with_reply_policy('人格',dict(affinity=95,familiarity=0,trust=20,
            nickname='NICK_SECRET',summary='MEMORY_SECRET',mood=dict(state='shy',intensity=2,reason='REASON_SECRET')))
        self.assertIn('保留当前好感对应的热度',text)
        self.assertIn('已有好感对应的热度保持',text)
        self.assertIn('对方明确认可的普通称呼可用',text)
        self.assertIn('不覆盖事实核验和必要信息',text)
        self.assertIn('有点害羞',text)
        for secret in ('NICK_SECRET','MEMORY_SECRET','REASON_SECRET'):
            self.assertNotIn(secret,text)
        self.assertNotIn('近期情绪',relation.with_reply_policy('',state(50,mood=dict(state=[],intensity=2))))

    def test_six_bands_have_no_subbands_or_default_neutral_mood(self):
        for lower,upper in ((0,19),(20,39),(40,59),(60,74),(75,89),(90,100)):
            text=relation.with_reply_policy('',state(lower))
            self.assertEqual(text,relation.with_reply_policy('',state(upper)))
            self.assertEqual(text,relation.with_reply_policy('',state(lower,mood=dict(state='neutral',intensity=2))))
            self.assertIn('明确痛苦或紧急求助在任何档位',text)
            self.assertIn('对话中你对别人的语气，不代表对这个人的语气',text)
        close=relation.with_reply_policy('',dict(affinity=95,familiarity=0,trust=50))
        self.assertNotIn('你们很熟',close)
        self.assertIn('不擅自亲昵称呼',close)

    def test_snapshot_does_not_change_memory_or_expose_evidence(self):
        self.memory.apply(memory_tests.suggestion(),self.snap(),NOW)
        p=self.memory.data['members']['wxid_a'];p['affinity']=82
        before=copy.deepcopy(self.memory.data)
        captured=self.memory.reply_relationship('wxid_a',NOW)
        self.assertEqual(captured['affinity'],82)
        self.assertEqual(set(captured),{'affinity','familiarity','trust'})
        self.assertIsNone(self.memory.reply_relationship('unknown',NOW))
        self.assertEqual(self.memory.data,before)

    def test_foreground_controller_captures_current_sender_relationship(self):
        self.memory.apply(memory_tests.suggestion(),self.snap(),NOW)
        self.memory.data['members']['wxid_a']['affinity']=95
        case=pipeline.PipelineTests('runTest');case.setUp();self.addCleanup(case.doCleanups)
        case.bot.memory=self.memory
        case.add(1,sender='wxid_a');case.bot.tick()
        worker=next(j for j in case.jobs if j.kind=='generate')
        self.assertIn('当前档位：很亲近',worker.payload[1][0]['content'])
        self.assertNotIn('wxid_a',worker.payload[1][0]['content'])

    def test_multimodal_rebuild_and_shared_output_rules_keep_one_relationship_policy(self):
        from wxbridge.ai.images import prepare_multimodal
        from wxbridge.ai.output import response_prompt
        case=media.LocalImageTests('runTest');case.setUp();self.addCleanup(case.doCleanups)
        case.path.write_bytes(media.png())
        generation=case.generation()
        trigger=generation['triggers'][-1]
        generation['memory']=dict(relationship_for=trigger['sender'],relationship=state(80))
        base=build_prompt(case.cfg,generation['context'],trigger,'合成群',memory=generation['memory'])
        rebuilt,info=prepare_multimodal(case.cfg,base,generation)
        final=response_prompt(rebuilt,case.cfg)
        self.assertEqual(info['images'],1)
        self.assertEqual(final[0]['content'].count(relation.REPLY_POLICY_START),1)
        self.assertIn('当前档位：熟悉亲近',final[0]['content'])

    def test_proactive_uses_selected_target_and_does_not_add_judgment_requests(self):
        rows=[msg(1,NOW,'刚才挺有意思','wxid_a'),msg(2,NOW,'我也觉得','wxid_b')]
        prompt=participation_prompt(self.cfg,rows,'合成群',opportunity=dict(mode='followup',reason='conversation',source_ids=[1,2]))
        client=Mock();client.complete.side_effect=[dict(content='{"speak":true,"reply_to":1}'),dict(content='有点意思')]
        with patch('wxbridge.ai.client.DeepSeekClient',return_value=client):
            result=evaluate(self.cfg,prompt,source_ids=[1,2],reply_relationships={'1':state(10),'2':state(95)})
        self.assertEqual(client.complete.call_count,2)
        self.assertNotIn(relation.REPLY_POLICY_START,client.complete.call_args_list[0].args[0][0]['content'])
        reply=client.complete.call_args_list[1].args[0][0]['content']
        self.assertIn('当前档位：明显疏远',reply)
        self.assertNotIn('当前档位：很亲近',reply)
        self.assertTrue(result['speak'])
