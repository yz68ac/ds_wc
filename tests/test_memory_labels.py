"""Stable memory ownership and temporary-label isolation; no real data or API."""
import copy
import unittest
import runpy
from pathlib import Path

import test_memory as memory_tests
from synthetic import NOW, msg
from wxbridge.ai.memory import snapshot
from wxbridge.ai.output import reply_rules


class MemoryLabelTests(unittest.TestCase):
    setUp = memory_tests.MemoryTests.setUp
    snap = memory_tests.MemoryTests.snap

    def test_new_summary_resolves_current_person_and_other_nickname(self):
        a=msg(2,NOW,'我喜欢简短回答','wxid_a');a['sender_name']='甲'
        b=msg(1,NOW,'这次我来解释','wxid_b');b['sender_name']='乙'
        value=snapshot(self.cfg,[a,b],[a],memory_tests.IDENTITY,self.memory,NOW)
        proposal=memory_tests.suggestion(source='m2')
        proposal['updates'][0]['memories'][0]['summary']='U1喜欢U2的解释方式'
        self.memory.apply(proposal,value,NOW)
        self.assertEqual(self.memory.data['members']['wxid_a']['entries'][0]['summary'],'本人喜欢乙的解释方式')

    def test_unknown_label_rejects_only_ambiguous_summary_and_advances_sources(self):
        proposal=memory_tests.suggestion()
        proposal['updates'][0]['memories'][0]['summary']='U9喜欢简洁回答'
        self.memory.apply(proposal,self.snap(),NOW)
        self.assertEqual(self.memory.data['members']['wxid_a']['entries'],[])
        self.assertEqual(self.memory.data['watermark'],1)
        self.assertEqual(self.memory.data['rejected_label_summaries'],1)

    def test_literal_technical_identifier_is_preserved_with_source_proof(self):
        value=self.snap();value['messages'][0]['text']='我正在调试芯片U1'
        proposal=memory_tests.suggestion(kind='event')
        proposal['updates'][0]['memories'][0]['summary']='正在调试芯片U1'
        self.memory.apply(proposal,value,NOW)
        entry=self.memory.data['members']['wxid_a']['entries'][0]
        self.assertEqual(entry['summary'],'正在调试芯片U1')
        self.assertEqual(entry['literal_labels'],['U1'])
        self.assertEqual(self.memory.status(NOW)['ambiguous_entries'],0)
        self.assertEqual(len(self.memory.background({'wxid_a':'U2'},NOW)['U2']['entries']),1)

    def test_old_ambiguous_summary_hidden_without_rebinding_or_deletion(self):
        self.memory.apply(memory_tests.suggestion(),self.snap(),NOW)
        person=self.memory.data['members']['wxid_a']
        person['entries'][0]['summary']='U2喜欢U1的回答'
        original=copy.deepcopy(person['entries'])
        self.assertEqual(self.memory.background({'wxid_a':'U2'},NOW)['U2']['entries'],[])
        prompt=self.memory.prompt({'wxid_a':'U2'},'wxid_a',NOW)
        self.assertNotIn('U2喜欢U1',prompt)
        self.memory.prune(NOW)
        self.assertEqual(person['entries'],original)
        self.assertEqual(self.memory.status(NOW)['ambiguous_entries'],1)
        self.memory.persist(NOW)
        self.assertIn('[隔离：旧临时编号无法还原]',self.memory.projection.read_text(encoding='utf-8'))

    def test_projection_distinguishes_profiles_from_learning_and_output_rules_are_shared(self):
        self.memory.apply(dict(updates=[]),self.snap(),NOW)
        text=self.memory.projection.read_text(encoding='utf-8')
        self.assertIn('成员档案：1；有可用摘要：0',text)
        self.assertEqual(len([s for s in text.splitlines() if s.startswith('## ')]),1)
        self.assertIn('# 语境表达学习（非成员档案',text)
        self.assertIn('不要在聊天正文中用这些编号称呼人',reply_rules(self.cfg))
        self.assertIn('真实技术编号',reply_rules(self.cfg))
        before=self.memory.path.read_bytes()
        self.memory.write_projection(NOW+1)
        self.assertEqual(self.memory.path.read_bytes(),before)

    def test_old_learning_and_relationship_prose_are_not_reinterpreted(self):
        self.memory.apply(memory_tests.suggestion(delta=1),self.snap(),NOW)
        p=self.memory.data['members']['wxid_a']
        p['affinity_evidence'][0]['reason']='U2对U1表示感谢'
        self.memory.data['learning']=dict(started_after_id=0,records=[dict(id='old',scope='member',
            owner='wxid_a',expression='[捂脸]',context='U1开玩笑',meaning='U2表示无奈',state='stable',
            basis=None,occurred_at=NOW,confirmed_at=NOW,evidence=[])])
        from wxbridge.storage import expression_memory
        before=copy.deepcopy(self.memory.data)
        self.assertEqual(expression_memory.background(self.memory.data,{'wxid_a':'U2'},NOW),[])
        self.assertEqual(expression_memory.prompt(self.memory.data,{'wxid_a':'U2'},'wxid_a','[捂脸]',300,NOW),[])
        background=self.memory.background({'wxid_a':'U2'},NOW)
        self.assertNotIn('U2对U1',str(background))
        self.assertEqual(self.memory.data,before)
        self.assertEqual(self.memory.status(NOW)['ambiguous_learning'],1)

    def test_new_relationship_and_expression_text_resolve_current_bindings(self):
        value=self.snap();value['messages'][0]['text']='谢谢你[捂脸]'
        proposal=memory_tests.suggestion(delta=1)
        proposal['updates'][0]['affinity']['reason']='U1表达感谢'
        proposal['learning']=[dict(scope='member',member='U1',expression='[捂脸]',context='U1道谢',
            meaning='U1带点不好意思',sources=['m1'],stance='support',replaces=None,basis=None)]
        value['learning_enabled']=True
        self.memory.apply(proposal,value,NOW)
        p=self.memory.data['members']['wxid_a']
        self.assertEqual(p['affinity_evidence'][0]['reason'],'本人表达感谢')
        record=self.memory.data['learning']['records'][0]
        self.assertEqual(record['context'],'本人道谢')
        self.assertEqual(record['meaning'],'本人带点不好意思')

    def test_deploy_pending_memory_opt_in_still_rejects_active_work(self):
        idle=runpy.run_path(str(Path(__file__).resolve().parents[1]/'scripts/deploy-release.py'))['listener_idle']
        pending=dict(pending=0,memory_pending=7)
        self.assertFalse(idle(pending,{}))
        self.assertTrue(idle(pending,{},allow_pending_memory=True))
        for queue,live in ((dict(pending=1,memory_pending=7),{}),(pending,dict(generation_workers=1)),
                           (pending,dict(memory=dict(running=True))),
                           (pending,dict(proactive=dict(outcome='evaluating')))):
            self.assertFalse(idle(queue,live,allow_pending_memory=True))
