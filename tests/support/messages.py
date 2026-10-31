"""Shared synthetic messages fixtures; contains no executable tests."""
from __future__ import annotations
import html
from wxbridge.wechat.store import WeChatStore
from wxbridge.wechat.messages import display_message
from tests.support.synthetic import NOW


PACKED_REFERENCE = (57 << 32) | 49


def quoted_xml(question="@ds 这怎么看", content="明天 10 点开会", kind=1, *, sender="wxid_original", created=NOW-10000):
    return (f'<msg><appmsg><title>{html.escape(question)}</title><type>57</type><refermsg>'
            f'<type>{kind}</type><svrid>1234567890123456789</svrid><fromusr>{sender}</fromusr>'
            f'<chatusr>synthetic@chatroom</chatusr><displayname>小明</displayname>'
            f'<content>{html.escape(content)}</content><createtime>{created}</createtime>'
            f'<msgsource>&lt;msgsource&gt;&lt;atuserlist&gt;wxid_self&lt;/atuserlist&gt;&lt;/msgsource&gt;</msgsource>'
            '</refermsg></appmsg></msg>')


def parse_message(xml, raw_type=PACKED_REFERENCE, mention="wxid_self"):
    store = WeChatStore.__new__(WeChatStore)
    row = {"local_id": 50, "local_type": raw_type, "create_time": NOW,
           "message_content": "wxid_friend:\n"+xml, "real_sender_id": 1,
           "source": f"<msgsource><atuserlist>{mention}</atuserlist></msgsource>"}
    return display_message(store._parse_message("synthetic@chatroom", row, {}, {1: "wxid_friend"}))
