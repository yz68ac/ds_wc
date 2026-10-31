"""Adaptive context regressions: synthetic SQLite, frozen tasks, no real APIs."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock

from tests.support.model_support import default_config
from tests.support.synthetic import NOW, msg, history_page
from wxbridge.ai.context import (ContextError, read_context, select_context, select_memory_context)
from wxbridge.ai.chat_payload import chat_payload, encode_payload, fit_payload
from wxbridge.ai.prompts import build_prompt
from wxbridge.ai.participation import build_prompt as judgment_prompt
from wxbridge.ai.prompt_parts import PromptComposer
from wxbridge.config import migrate_context_data, resolve_config
from wxbridge.wechat.store import WeChatStore
import tests.support.conversation as conversation
import tests.support.private as private


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.cfg = default_config()
        self.source = msg(100, NOW, '当前问题')

    def test_latest_thirty_and_older_bot_neighbors_are_deduplicated(self):
        rows = [msg(i, NOW-1000+i, str(i), 'bot' if i in (10,20,30,60) else 'person') for i in range(1,81)]
        self.cfg['target']['context_recent_messages'] = 3
        saved = deepcopy(rows)
        chosen = select_context(self.cfg, rows, self.source, account='bot')
        self.assertEqual(chosen.recent_ids, (78,79,80))
        self.assertEqual(chosen.bot_groups, (tuple(range(18,23)),tuple(range(28,33)),tuple(range(58,63))))
        self.assertEqual(len(chosen),18)
        self.assertNotIn(10,[m['local_id'] for m in chosen])
        self.assertEqual(rows,saved)
        self.assertEqual(len(select_context(self.cfg,rows,self.source)),3)
        self.cfg['target']['context_bot_messages'] = 0
        self.assertEqual(len(select_context(self.cfg,rows,self.source,account='bot')),3)

    def test_slow_chat_boundary_and_watermark_exclude_later_backfilled_messages(self):
        rows = [msg(1,NOW-1801,'expired'),msg(2,NOW-1800,'boundary'),msg(3,NOW-720,'twelve minutes'),
                msg(99,NOW,'same second before'),msg(101,NOW,'same second after'),
                msg(102,NOW-500,'backfilled after source'),msg(4,NOW+1,'future'),
                dict(msg(5,NOW-10,'observed'),time_source='observed')]
        chosen = select_context(self.cfg,rows,self.source)
        self.assertEqual([m['local_id'] for m in chosen],[2,3,99])
        memory = select_memory_context(self.cfg,rows,self.source)
        self.assertEqual([m['local_id'] for m in memory],[99])

    def test_scan_cap_counts_raw_nontext_records_and_stops_at_five_pages(self):
        rows = [dict(msg(i,NOW-1500+i,'image'),type=47) for i in range(1,1201)]
        store = Mock()
        store.messages_before.side_effect = lambda chat, **kw: history_page(rows,**kw)
        selected = read_context(self.cfg,store,'synthetic',[msg(2000,NOW,'source')],account='bot')
        self.assertEqual(selected,[])
        self.assertEqual((selected.scanned,selected.scan_limit_hit),(1000,True))
        self.assertEqual(store.messages_before.call_count,5)
        self.assertTrue(all(call.kwargs['limit']==200 for call in store.messages_before.call_args_list))

    def test_scan_finishes_early_when_recent_and_bot_neighbors_are_available(self):
        rows = [msg(i,NOW-1100+i,str(i),'bot' if i in (900,910,920) else 'person') for i in range(1,1000)]
        store = Mock()
        store.messages_before.side_effect = lambda chat, **kw: history_page(rows,**kw)
        selected = read_context(self.cfg,store,'synthetic',[msg(1000,NOW,'source')],account='bot')
        self.assertEqual(store.messages_before.call_count,1)
        self.assertEqual((selected.scanned,selected.scan_limit_hit),(200,False))
        self.assertLessEqual(len(selected),45)

    def test_both_stages_share_selection_and_snapshot_survives_later_changes(self):
        rows = [msg(i,NOW-900+i,'历史'+str(i),'bot' if i==5 else 'person') for i in range(1,60)]
        store = Mock()
        store.messages_before.side_effect = lambda chat, **kw: history_page(rows,**kw)
        selection = read_context(self.cfg,store,'synthetic',[self.source],account='bot')
        reply = build_prompt(self.cfg,selection,self.source,'合成群',account='bot')
        decision = judgment_prompt(self.cfg,selection.include([self.source]),'合成群',source_ids=[100],account='bot')
        self.assertEqual(reply[1]['content'],decision[1]['content'])
        saved = deepcopy(reply)
        rows[0]['text']='CHANGED';rows.append(msg(101,NOW+1,'FUTURE'))
        self.assertEqual(reply,saved)
        self.assertEqual(reply.context_stats,decision.context_stats)
        self.assertNotIn('recent_ids',reply[1]['content'])


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.cfg = default_config()
        self.cfg['target'].update(context_recent_messages=2,context_bot_messages=2)
        self.rows = [msg(i,NOW-100+i,'完整历史'+str(i)+'甲'*200,'bot' if i in (5,15) else 'person') for i in range(1,26)]
        self.source = msg(30,NOW,'当前问题')
        self.source['reference'] = dict(kind='image',sender='person',available=False,text='',image_md5='a'*32)
        self.selection = select_context(self.cfg,self.rows,self.source,account='bot')
        self.payload = chat_payload([*self.selection,self.source],'合成群',[30],account='bot')

    def without(self, identifiers):
        result = deepcopy(self.payload)
        result['messages'] = [m for m in result['messages'] if m['id'] not in identifiers]
        result['history_omitted'] = len(identifiers)
        return result

    def test_old_supplement_group_drops_before_newer_group_and_recent_messages(self):
        expected = self.without(set(range(3,8)))
        fitted = fit_payload(self.payload,len(encode_payload(expected)),plan=self.selection)
        self.assertEqual(fitted,expected)
        self.assertEqual(fitted['messages'][-1]['reference'],self.payload['messages'][-1]['reference'])

    def test_overlapping_group_members_are_kept_for_the_newer_group(self):
        rows = [msg(i,NOW-30+i,'x'*100,'bot' if i in (5,6) else 'person') for i in range(1,11)]
        selection = select_context(self.cfg,rows,self.source,account='bot')
        payload = chat_payload([*selection,self.source],'合成群',[30],account='bot')
        expected = deepcopy(payload)
        expected['messages'] = [m for m in payload['messages'] if m['id']!=3]
        expected['history_omitted']=1
        self.assertEqual(fit_payload(payload,len(encode_payload(expected)),plan=selection),expected)

    def test_optional_profile_and_memory_cannot_displace_history(self):
        value = deepcopy(self.payload)
        value['summary'] = 'LONG_SUMMARY'*1000
        value['members']['person']['memory'] = ['LONG_MEMORY'*1000]
        value['memory']=['TOP_LEVEL_MEMORY'*1000]
        fitted = fit_payload(value,len(encode_payload(self.payload)),plan=self.selection)
        self.assertEqual(fitted,self.payload)

    def test_media_rebinding_reuses_priority_without_resurrecting_removed_history(self):
        self.cfg['target']['context_max_chars'] = len(encode_payload(self.without(set(range(3,8)))))
        prompt = build_prompt(self.cfg,self.selection,self.source,'合成群',account='bot')
        before = json.loads(prompt[1]['content'])
        changed = deepcopy(self.source)
        changed['reference'].update(available=True,text='图片来源说明'*100)
        rebound = PromptComposer.references(prompt,[changed],account='bot')
        after = json.loads(rebound[1]['content'])
        self.assertTrue({m['id'] for m in after['messages']} <= {m['id'] for m in before['messages']})
        self.assertEqual(after['history_omitted'],10)
        self.assertEqual([m['id'] for m in after['messages']],[24,25,30])
        self.assertLessEqual(rebound.context_stats['chars'],self.cfg['target']['context_max_chars'])

    def test_required_quote_overflow_has_specific_nonretryable_code_even_in_strict_mode(self):
        self.source['reference']['text'] = '长引用'*6000
        for strategy in ('recent','strict'):
            self.cfg['target']['context_budget_strategy']=strategy
            with self.assertRaises(ContextError) as caught:
                build_prompt(self.cfg,[],self.source,'合成群',account='bot')
            self.assertEqual(caught.exception.code,'context_required_too_large')


class QueryAndMigrationTests(unittest.TestCase):
    def test_sql_cursor_watermark_limit_and_normalization(self):
        root = Path(__file__).resolve().parents[1]/'.test-tmp'
        with tempfile.TemporaryDirectory(dir=root) as tmp:
            filename = str(Path(tmp)/'messages.db')
            with sqlite3.connect(filename) as conn:
                conn.execute('CREATE TABLE Msg_test(local_id INTEGER,local_type INTEGER,create_time INTEGER,message_content TEXT)')
                conn.executemany('INSERT INTO Msg_test VALUES (?,1,?,?)',[
                    (1,NOW-1801,'wxid_a:\nexpired'),(2,NOW-1800,'wxid_a:\nboundary'),
                    (3,NOW,'wxid_a:\nearly'),(4,NOW,'wxid_a:\nsource'),
                    (5,NOW,'wxid_a:\nlater'),(6,NOW-1,'wxid_a:\nbackfill')])
            conn.close()
            store = WeChatStore.__new__(WeChatStore)
            store.message_table = Mock(return_value=('test.db','Msg_test'))
            store.contacts = Mock(return_value={})
            def connect(*args):
                conn = sqlite3.connect(filename);conn.row_factory=sqlite3.Row
                return conn
            store._connect=connect
            first=store.messages_before('synthetic@chatroom',before=(NOW,4),since=NOW-1800,max_local_id=4,limit=1)
            self.assertEqual((first.scanned,first.cursor),(1,(NOW,3)))
            second=store.messages_before('synthetic@chatroom',before=first.cursor,since=NOW-1800,max_local_id=4,limit=200)
            self.assertEqual([m['local_id'] for m in second.messages],[2])
            self.assertEqual(first.messages[0]['sender'],'wxid_a')

    def test_explicit_migration_is_idempotent_and_conflicts_are_rejected(self):
        old=dict(target=dict(context_window_seconds=600),memory=dict(enabled=False),enabled=False,chat='synthetic@chatroom')
        saved=deepcopy(old)
        new=migrate_context_data(old)
        self.assertEqual(old,saved)
        self.assertEqual(new['target'],{})
        self.assertEqual(new['memory'],dict(enabled=False,context_window_seconds=600))
        self.assertEqual(migrate_context_data(new),new)
        with self.assertRaisesRegex(ValueError,'conflicting_memory_context_window'):
            migrate_context_data(dict(target=dict(context_window_seconds=600),memory=dict(context_window_seconds=300)))
        with self.assertRaisesRegex(ValueError,'obsolete_model_configuration'):
            resolve_config(dict(target=dict(context_window_seconds=300)),'synthetic.json',environment=False,validate_role_models=False)
        for key in ('context_recent_messages','context_max_age_seconds','context_bot_messages','context_max_chars'):
            with self.subTest(key=key),self.assertRaises(ValueError):
                resolve_config(dict(target={key:True}),'synthetic.json',environment=False,validate_role_models=False)


class PipelineTests(unittest.TestCase):
    def fixture(self, cls):
        case=cls();case.setUp();self.addCleanup(case.doCleanups)
        return case

    def test_oversized_decision_batch_is_split_and_bad_single_does_not_hold_next_candidate(self):
        case=self.fixture(conversation.ConversationFixture)
        case.add(1,text='甲'*16000,mention=False)
        case.add(2,text='另一个正常问题',mention=False)
        case.bot.tick()
        self.assertFalse(case.decision_jobs)
        self.assertEqual(case.bot.journal.participation_status()['last_error']['code'],'context_required_too_large')
        case.bot.tick()
        self.assertEqual(case.bot.participation.pending['source_ids'],[2])

    def test_private_required_overflow_sends_notice_through_confirmation_without_model_retry(self):
        case=self.fixture(private.PrivateReplyFixture)
        case.add(1,text='甲'*16000,mention=False)
        case.bot.tick()
        self.assertFalse([j for j in case.jobs if j.kind=='generate'])
        self.assertFalse(case.decision_jobs)
        self.assertEqual(case.sends()[0].payload[2],'消息或引用内容过长，请分开发送。')
        task=case.bot.journal.task(1)
        self.assertEqual(task['last_generation_error'],'context_required_too_large')
        case.sends()[0].result=dict(ok=True,ui=dict(operation=dict(submitted=True)))
        case.add(2,sender='wxid_self',text=case.sends()[0].payload[2],mention=False)
        case.bot.tick()
        self.assertEqual(case.bot.journal.task(1)['phase'],'done')
        self.assertEqual(len(case.sends()),1)


if __name__=='__main__':
    unittest.main()
