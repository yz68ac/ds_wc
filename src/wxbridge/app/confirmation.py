"""Bounded outbound-record reconciliation, independent of bot queues or AI tasks."""
from wechat_linux.errors import StoreError


def scan_confirmation(store,chat_id,account,text,intent,now,*,blocked=False,image=None):
    scans=[('scan_cursor',max(intent['baseline'],intent.get('scan_cursor',intent['baseline'])-20),2)]
    if blocked and now>=intent.get('audit_at',0):
        scans.append(('audit_cursor',intent.get('audit_cursor',intent['baseline']),1))
    for field,cursor,pages in scans:
        for _ in range(pages):
            rows=store.messages_after(chat_id,cursor,limit=100)
            for message in rows:
                matches = (message.get('type') == 3 and message.get('image_md5') == image['md5']) if image else (
                    message.get('type') == 1 and message.get('text') == text)
                if (message['local_id']>intent['baseline'] and message.get('sender')==account and matches):
                    return message['local_id']
            if rows:
                next_cursor=max(m['local_id'] for m in rows)
                if next_cursor<=cursor and len(rows)>=100:raise StoreError('confirmation_cursor_stalled')
                cursor=max(cursor,next_cursor)
            intent[field]=cursor
            if len(rows)<100:
                if field=='audit_cursor':
                    intent.pop('audit_cursor',None);intent['audit_at']=now+60
                break
    return None
