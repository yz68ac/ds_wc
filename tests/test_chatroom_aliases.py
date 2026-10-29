"""Bounded protobuf fixtures; no real contact database."""
import unittest

from wechat_linux.messages.chatrooms import parse_member_aliases


def field(number, value):
    data = value.encode() if isinstance(value, str) else value
    assert len(data) < 128
    return bytes([number*8+2, len(data)]) + data


class ChatroomAliasTests(unittest.TestCase):
    def test_cards_missing_cards_unknown_fields_and_memoryview(self):
        data = field(1, field(1, 'wxid_a') + field(2, '群名片'))
        data += field(1, field(1, 'wxid_b') + b'\x18\x01')
        data += field(9, 'unknown')
        self.assertEqual(parse_member_aliases(memoryview(data)), {'wxid_a': '群名片', 'wxid_b': ''})

    def test_inconsistent_or_truncated_encoding_never_partially_updates_members(self):
        valid = field(1, field(1, 'wxid_a'))
        cases = [valid + b'\x0a\x08x', valid + valid,
            field(1, field(1, 'wxid_a') + field(1, 'wxid_b')),
            field(1, field(1, 'wxid_a') + field(2, b'\xff')),
            field(1, field(1, 'group@chatroom')), b'\x08\x80', b'\x00', b'\x0b']
        for data in cases:
            with self.subTest(data=data):
                self.assertEqual(parse_member_aliases(data), {})

    def test_unbounded_or_nonbinary_input_is_ignored(self):
        for data in (None, 'text', b'x'*(2*1024*1024+1)):
            with self.subTest(kind=type(data).__name__):
                self.assertEqual(parse_member_aliases(data), {})
