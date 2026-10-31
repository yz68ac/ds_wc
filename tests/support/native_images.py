"""Shared synthetic native_images fixtures; contains no executable tests."""


CHAT, ACCOUNT, SENDER, DIGEST = 'synthetic@chatroom', 'wxid_synthetic', 'wxid_person', 'a'*32

THUMB = (b'synthetic-thumbnail', dict(quality='thumbnail', width=10, height=20))


FULL = (b'synthetic-clear-cache', dict(quality='cached', width=100, height=200))


COMPLETE = dict(ok=True, state='native_completed', requested=True, source_verified=True, download_status=[0, 0])


DONE = dict(stage='done', pinned=False, native_inflight=False, callback_references=0,
            cleanup=True, message_verified=True, coroutine_completion_state=1,
            download_call_returned=True, download_future_state=1, download_status=[0, 0], downloads=1)


def value():
    return dict(op='download_image', request_id='image:one', account=ACCOUNT, chat_id=CHAT,
                server_id='18446744073709551615', sender=SENDER, image_md5=DIGEST, timeout_seconds=5)


def binding():
    return dict(account=ACCOUNT, account_root='/synthetic/private', candidates=[],
                download=dict(verified=True, chat_id=CHAT, server_id=value()['server_id'], sender=SENDER, image_md5=DIGEST))
