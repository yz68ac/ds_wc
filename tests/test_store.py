"""使用合成数据的离线测试，不联网、不发送微信消息。"""
from __future__ import annotations

import unittest
import sqlite3
from unittest.mock import Mock
from wxbridge.wechat.store import WeChatStore, parse_mentioned_users
from tests.support.synthetic import NOW


class QueryTests(unittest.TestCase):
    def test_image_query_preserves_validated_local_cache_key(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute('CREATE TABLE Msg_test (local_id INTEGER,local_type INTEGER,create_time INTEGER,message_content TEXT,packed_info_data BLOB)')
        conn.execute('INSERT INTO Msg_test VALUES (1,3,?,?,?)', (NOW, '<msg><img md5="'+"a"*32+'"/></msg>', b'\x1a\x22\x22\x20'+b'b'*32))
        store = WeChatStore.__new__(WeChatStore)
        store.message_table = Mock(return_value=('test.db', 'Msg_test'))
        store.contacts = Mock(return_value={})
        store._connect = Mock(return_value=conn)
        record = store.messages_in_window('synthetic@chatroom', NOW-600, NOW)[0]
        self.assertEqual(record['image_md5'], 'a'*32)
        self.assertEqual(record['image_cache_key'], 'b'*32)
        self.assertNotIn('packed_info_data', record)

    def test_real_sender_mapping_without_sender_prefix(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute('CREATE TABLE Msg_test (local_id INTEGER,local_type INTEGER,create_time INTEGER,message_content TEXT,real_sender_id INTEGER)')
        conn.execute('CREATE TABLE Name2Id (user_name TEXT)')
        conn.execute("INSERT INTO Name2Id VALUES ('wxid_self')")
        conn.execute('INSERT INTO Msg_test VALUES (1,1,?,?,1)', (NOW, '正文没有发送者前缀'))
        store = WeChatStore.__new__(WeChatStore)
        store.db_dir = '/synthetic/wxid_self_abcd/db_storage'
        store.message_table = Mock(return_value=('test.db', 'Msg_test'))
        store.contacts = Mock(return_value={'wxid_self': {'nick_name': '自己的账号'}})
        store._connect = Mock(return_value=conn)
        self.assertEqual(store.self_wxid, 'wxid_self')
        records = store.messages_in_window('synthetic@chatroom', NOW-600, NOW)
        self.assertEqual(records[0]['sender'], store.self_wxid)
        self.assertEqual(records[0]['sender_name'], '自己的账号')
        self.assertEqual(records[0]['text'], '正文没有发送者前缀')

    def test_sql_bounds_with_both_schema_variants(self):
        for has_content_type in (False, True):
            with self.subTest(has_content_type=has_content_type):
                conn = sqlite3.connect(":memory:")
                conn.row_factory = sqlite3.Row
                extra = ", WCDB_CT_message_content INTEGER" if has_content_type else ""
                conn.execute("CREATE TABLE Msg_test (local_id INTEGER, local_type INTEGER, "
                             "create_time INTEGER, message_content TEXT" + extra + ")")
                for number, sent in [(1, NOW - 601), (2, NOW - 600), (3, NOW), (4, NOW + 1)]:
                    conn.execute("INSERT INTO Msg_test (local_id,local_type,create_time,message_content) "
                                 "VALUES (?,1,?,?)", (number, sent, f"wxid_friend:\n记录{number}"))
                store = WeChatStore.__new__(WeChatStore)
                store.db_dir = "/synthetic/wxid_self/db_storage"
                store.message_table = Mock(return_value=("test.db", "Msg_test"))
                store.contacts = Mock(return_value={})
                store._connect = Mock(return_value=conn)
                selected = store.messages_in_window("synthetic@chatroom", NOW - 600, NOW)
                self.assertEqual([m["local_id"] for m in selected], [2, 3])
                self.assertTrue(all(m["time_source"] == "database" for m in selected))


class MentionMetadataTests(unittest.TestCase):
    def test_xml_cdata_and_exact_user_ids(self):
        self.assertEqual(parse_mentioned_users('<msgsource><atuserlist><![CDATA[wxid_one, wxid_two]]></atuserlist></msgsource>'),
                         ['wxid_one', 'wxid_two'])
        self.assertEqual(parse_mentioned_users('<msgsource/>'), [])
        self.assertIsNone(parse_mentioned_users(''))
        self.assertIsNone(parse_mentioned_users('invalid xml'))


if __name__ == "__main__":
    unittest.main()
