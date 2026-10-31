"""Bot adapter for the model-independent local WeChat server."""
import time
from wxbridge.native.protocol import NativeError, REQUEST_ID, settings, validate_send


class BotNativeService:
    def __init__(self,bot,options=None):
        self.bot=bot
        self.cfg=settings(bot.cfg) if options is None else options

    def handle(self, value):
        if value == {'op':'info'}:
            return dict(enabled=True, transport='native_service', normal_delivery=self.bot.cfg['ui']['backend'], allowed_chat_ids=self.cfg['allowed_chat_ids'],
                        max_text_bytes=self.cfg['max_text_bytes'])
        if isinstance(value, dict) and value.get('op') == 'status':
            identifier = value.get('request_id')
            if set(value) != {'op','request_id'} or not isinstance(identifier,str) or not REQUEST_ID.fullmatch(identifier):
                raise NativeError('invalid_request_id')
            return self.bot.journal.external_status(identifier)
        value = validate_send(value, self.cfg['max_text_bytes'])
        if value['chat_id'] not in self.cfg['allowed_chat_ids']:
            raise NativeError('chat_not_allowed')
        # Duplicate requests are resolved before any fresh DB/UI work or pending limit checks.
        duplicate = self.bot.journal.external_duplicate(value)
        if duplicate is not None:
            return duplicate
        identifier = value['chat_id']
        if identifier == 'filehelper':
            if identifier not in self.bot.store.contacts():
                raise NativeError('chat_not_found')
            title = 'File Transfer'
        elif identifier == self.bot.chat_id:
            title = self.bot.chat_title
        else:
            resolved, title = self.bot.store.resolve_chat(identifier)
            if resolved != identifier:
                raise NativeError('chat_not_found')
        # Make sure this target has readable local history before accepting a task.
        # This first trial deliberately requires a known existing message table.
        self.bot.store.last_local_id(identifier)
        return self.bot.journal.enqueue_external(value, title, time.time(),
            max_pending=self.cfg['max_pending'], max_requests=self.cfg['max_requests'])


class AccountNativeService:
    """One socket and account-wide request IDs, routed to each owning journal."""
    def __init__(self, coordinator, options):
        self.sessions, self.cfg = coordinator.sessions, options
        self.account = coordinator.account

    def handle(self, value):
        if value == {'op': 'info'}:
            return dict(enabled=True, transport='native_service', normal_delivery='native_service',
                        allowed_chat_ids=self.cfg['allowed_chat_ids'], max_text_bytes=self.cfg['max_text_bytes'])
        if isinstance(value, dict) and value.get('op') == 'status':
            identifier = value.get('request_id')
            if set(value) != {'op', 'request_id'} or not isinstance(identifier, str) or not REQUEST_ID.fullmatch(identifier):
                raise NativeError('invalid_request_id')
            for session in self.account.histories(requests_only=True):
                try:
                    return session.journal.external_status(identifier)
                except NativeError as exc:
                    if exc.code != 'request_not_found':
                        raise
            raise NativeError('request_not_found')
        value = validate_send(value, self.cfg['max_text_bytes'])
        if value['chat_id'] not in self.cfg['allowed_chat_ids']:
            raise NativeError('chat_not_allowed')
        for session in self.account.histories(requests_only=True):
            duplicate = session.journal.external_duplicate(value)
            if duplicate is not None:
                return duplicate
        target = next((s for s in self.sessions if s.chat_id == value['chat_id'] and not getattr(s,'quiescing',False)), None)
        if target is None:
            # Unmanaged groups must not silently share somebody else's state.
            raise NativeError('chat_instance_not_enabled')
        if sum(s.journal.pending_count() for s in self.sessions) >= self.cfg['max_pending']:
            raise NativeError('queue_full')
        count = sum(s.journal.external_request_count() for s in self.account.histories(requests_only=True))
        if count >= self.cfg['max_requests']:
            raise NativeError('request_history_full')
        return BotNativeService(target, self.cfg).handle(value)
