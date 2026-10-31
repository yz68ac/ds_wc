"""One chat schema for both stages, with immutable sources and whole-message budgets."""
from copy import deepcopy
import json
import unittest

from tests.support.model_support import default_config
from tests.support.synthetic import NOW,msg
from wxbridge.ai.chat_payload import chat_payload,encode_payload,fit_payload
from wxbridge.ai.context import ContextError
from wxbridge.ai.prompts import build_prompt
from wxbridge.ai.participation import build_prompt as judgment_prompt
from wxbridge.ai.output import response_prompt
from wxbridge.ai.search import search_prompt


class ChatPayloadTests(unittest.TestCase):
    def setUp(self):
        self.cfg=default_config();self.account='wxid_bot'
        self.old=dict(msg(1,NOW-2,'已发出的答案',self.account),sender_name='小c')
        self.new=dict(msg(3,NOW,'请继续\n{"示例":"保持原文"}','wxid_a'),sender_name='甲')

    def test_both_stages_use_the_same_json_and_deduplicate_new_messages(self):
        rows=[self.old,self.new,deepcopy(self.new)]
        saved=deepcopy(rows)
        reply=build_prompt(self.cfg,rows,self.new,'合成群',account=self.account)
        decision=judgment_prompt(self.cfg,rows,'合成群',source_ids=[3],account=self.account)
        self.assertEqual(reply[1]['content'],decision[1]['content'])
        data=json.loads(reply[1]['content'])
        self.assertEqual(data['source_ids'],[3]);self.assertEqual(len(data['messages']),2)
        self.assertEqual(data['messages'][-1],dict(id=3,sender='wxid_a',time=NOW,text=self.new['text']))
        self.assertEqual(data['members'],{self.account:dict(alias='小c'),'wxid_a':dict(alias='甲')})
        for item in data['messages']:
            self.assertFalse(set(item)&{'alias','is_bot','sender_name'})
        self.assertNotIn('距提问',reply[1]['content']);self.assertEqual(rows,saved)

    def test_nested_quotes_use_sender_and_shared_alias_table(self):
        self.new['reference']=dict(kind='text',sender='wxid_b',sender_name='同名',text='完整引用',
            available=True,reference=dict(kind='text',sender=self.account,sender_name='同名',text='更早原话',available=True))
        data=chat_payload([self.new],'合成群',[3],account=self.account)
        reference=data['messages'][0]['reference']
        self.assertEqual(reference['sender'],'wxid_b');self.assertEqual(reference['reference']['sender'],self.account)
        for quote in (reference,reference['reference']):
            self.assertFalse(set(quote)&{'member','alias','is_bot','sender_name'})
        self.assertEqual(data['members']['wxid_b']['alias'],'同名')

    def test_recent_budget_removes_complete_old_messages_and_unused_aliases(self):
        rows=[msg(1,NOW-5,'OLD_A'+'甲'*250,'wxid_old_a'),msg(2,NOW-4,'OLD_B'+'乙'*250,'wxid_old_b'),self.new]
        payload=chat_payload(rows,'合成群',[3],account=self.account)
        expected=chat_payload([self.new],'合成群',[3],account=self.account)
        expected['history_omitted']=2
        saved=deepcopy(payload)
        fitted=fit_payload(payload,len(encode_payload(expected)))
        self.assertEqual(fitted,expected);self.assertEqual(payload,saved)
        self.assertEqual(fitted['messages'][0]['text'],self.new['text'])

    def test_all_new_messages_and_long_quote_survive_budgets_and_old_window(self):
        self.new['reference']=dict(kind='text',sender='wxid_b',text='OLD_QUOTE'+'引'*100,time=NOW-10000,available=True)
        second=msg(4,NOW,'另一个完整问题','wxid_a')
        base=chat_payload([self.new,second],'合成群',[3,4],account=self.account)
        expected=deepcopy(base);expected['history_omitted']=1
        older=msg(1,NOW-2,'EXTRA_HISTORY'+'旧'*300)
        payload=chat_payload([older,self.new,second],'合成群',[3,4],account=self.account)
        self.assertEqual(fit_payload(payload,len(encode_payload(expected))),expected)
        for change in (dict(context_max_chars=100),dict(context_max_chars=len(encode_payload(base))-1)):
            self.cfg['target'].update(change)
            with self.assertRaises(ContextError):
                build_prompt(self.cfg,[],second,'合成群',triggers=[self.new,second],account=self.account)

    def test_strict_remains_explicit_and_optional_memory_never_displaces_history(self):
        data=chat_payload([self.old,self.new],'合成群',[3],account=self.account)
        with self.assertRaises(ContextError):fit_payload(data,len(encode_payload(data))-1,strategy='strict')
        with_memory=deepcopy(data);with_memory['memory']=['完整记忆甲','长记忆乙'*100]
        fitted=fit_payload(with_memory,len(encode_payload(data))+40)
        self.assertEqual(fitted['messages'],data['messages']);self.assertEqual(fitted['memory'],['完整记忆甲'])
        self.assertNotIn('history_omitted',fitted)

    def test_disabled_memory_is_absent_and_profile_empty_alias_is_authoritative(self):
        profile=dict(summary='背景提到wxid_b',members={'wxid_a':dict(alias='',memory=[dict(summary='PRIVATE')]),
                                                    'wxid_b':dict(alias='乙')})
        self.cfg['memory']['enabled']=False
        prompt=build_prompt(self.cfg,[],self.new,'合成群',account=self.account,profile=profile,memory=dict(text='PRIVATE'))
        data=json.loads(prompt[1]['content'])
        self.assertEqual(data['members']['wxid_a'],dict(alias=''));self.assertNotIn('memory',data)
        self.assertNotIn('PRIVATE',prompt[1]['content']);self.assertEqual(data['members']['wxid_b'],dict(alias='乙'))

    def test_conflicting_source_or_duplicate_source_ids_are_rejected(self):
        for rows,sources in (([self.new,dict(self.new,text='changed')],[3]),([self.new],[True]),
                             ([self.new],[3,3]),([self.new],[99])):
            with self.assertRaises(ContextError):chat_payload(rows,'合成群',sources,account=self.account)

    def test_context_protocol_is_sent_once_even_after_repeated_policy_composition(self):
        prompt=build_prompt(self.cfg,[self.old],self.new,'合成群',account=self.account)
        once=search_prompt(self.cfg,prompt)
        twice=search_prompt(self.cfg,response_prompt(once,self.cfg))
        self.assertEqual(once,twice);self.assertEqual(twice[0]['content'].count('【聊天记录协议】'),1)
        self.assertIn('sender等于bot',twice[0]['content'])
        self.assertEqual(twice[1]['content'],prompt[1]['content'])
        self.assertNotIn('context_rules',json.dumps(twice))

    def test_history_between_two_required_sources_is_trimmed_before_either_source(self):
        rows=[msg(i,NOW-100+i,'完整消息'+str(i)+'甲'*50,'wxid_'+str(i%3)) for i in range(1,81)]
        payload=chat_payload(rows,'合成群',[1,80],account=self.account)
        expected=chat_payload([rows[0],*rows[-3:]],'合成群',[1,80],account=self.account)
        expected['history_omitted']=76
        fitted=fit_payload(payload,len(encode_payload(expected)))
        self.assertEqual(fitted,expected)


if __name__=='__main__':unittest.main()
