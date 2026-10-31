"""Group-aware boot, release and CLI metadata, using private synthetic files."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import Mock, patch

from tests.support.instances import InstanceFixture, CHATS
from wxbridge.app.operations import configured_groups, live_status_path
from wxbridge.storage.journal import Journal
import tests.support.boot as test_boot_recovery

spec = importlib.util.spec_from_file_location('group_deployment', Path(__file__).resolve().parents[1]/'scripts/deploy-release.py')
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)
boot = test_boot_recovery.boot


class InstanceOperationsTests(InstanceFixture):
    def setUp(self):
        super().setUp()
        self.runtime = self.root/'runtime'; self.runtime.mkdir(mode=0o700)
        self.persist = self.root/'persist'; self.persist.mkdir(mode=0o700)
        self.data['queue']['file'] = str(self.runtime/'queue.sqlite3')
        self.data['state_file'] = str(self.runtime/'state.json')
        self.data['native_api'] = dict(enabled=True, allowed_chat_ids=list(CHATS))
        self.data['ui'] = dict(backend='native_service')
        self.cfg = self.load()
        for target, value in ((boot, 'APP'), (deploy, 'APP')):
            p = patch.object(target, value, self.root); p.start(); self.addCleanup(p.stop)
        for target in (boot, deploy):
            p = patch.object(target, 'RUN', self.runtime); p.start(); self.addCleanup(p.stop)
        p = patch.object(boot, 'PERSIST', self.persist); p.start(); self.addCleanup(p.stop)
        p = patch('wxbridge.storage.journal.checked_path', side_effect=lambda v, **kw:Path(v))
        p.start(); self.addCleanup(p.stop)

    def create_queues(self):
        for i, entry in enumerate(self.cfg['instances']):
            path = Path(entry['config']['queue']['file']); path.parent.mkdir(parents=True, mode=0o700)
            journal = Journal(str(path), dict(account='wxid_bot', chat=entry['chat']), i+5)
            with journal.db:
                journal.enqueue_memory('activity', f'synthetic:{i}', [dict(local_id=1, time=123)], [0, 123])
            journal.close()

    def test_release_reads_all_groups_and_uses_account_status(self):
        self.create_queues()
        value = deploy.queue()
        self.assertEqual(value['cursor'], dict(alpha=5, beta=6))
        self.assertEqual(value['pending'], 0)
        self.assertEqual(value['memory_pending'], 2)
        self.assertEqual(live_status_path(self.root, self.runtime), self.runtime/'account-status.json')
        self.assertFalse(deploy.listener_idle(value, {}, allow_pending_memory=False))
        self.assertTrue(deploy.listener_idle(value, {}, allow_pending_memory=True))

    def test_checkpoint_all_groups_is_metadata_only_and_preserves_each_identity(self):
        self.create_queues()
        with patch.object(boot.os, 'geteuid', return_value=1000, create=True):
            value = boot.checkpoint()
        self.assertTrue(value['saved'])
        for i, entry in enumerate(self.cfg['instances']):
            path = self.persist/'instances'/entry['id']/'cursor.json'
            result = json.loads(path.read_text())
            self.assertEqual(result['cursor'], i+5)
            self.assertEqual(result['identity']['chat'], entry['chat'])
            for secret in ('ALPHA_PERSONA', 'api_key', 'triggers', 'PRIVATE_CHAT'):
                self.assertNotIn(secret, path.read_text())

    def test_probe_uses_one_store_and_restores_each_group_cursor(self):
        for i, entry in enumerate(self.cfg['instances']):
            path = self.persist/'instances'/entry['id']/'cursor.json'
            path.parent.mkdir(parents=True, mode=0o700)
            path.write_text(json.dumps(dict(schema=1, identity=dict(account='wxid_bot', chat=entry['chat']), cursor=i+5)))
        store = Mock()
        store.self_wxid = 'wxid_bot'
        store.resolve_chat.side_effect = lambda chat:(chat, '合成群')
        store.last_local_id.return_value = 20
        with patch('wxbridge.wechat.store.WeChatStore', return_value=store) as factory, \
             patch('wxbridge.native.sender.readiness'), \
             patch('wxbridge.storage.memoryfs.require_memory_directory', side_effect=lambda p:Path(p).mkdir(parents=True, exist_ok=True, mode=0o700)):
            result = boot.probe()
        factory.assert_called_once()
        self.assertTrue(result['ready'])
        for i, entry in enumerate(self.cfg['instances']):
            state = json.loads(Path(entry['config']['state_file']).read_text())
            self.assertEqual(state['chats'][entry['chat']]['last_local_id'], i+5)

    def test_instance_events_remain_parseable_and_filter_out_other_groups(self):
        import logging
        from wxbridge.app.logging_context import InstanceLog
        from wxbridge.app.monitor import safe_events
        path = self.root/'events.log'
        logger = logging.getLogger('synthetic-instance-events')
        logger.setLevel(logging.INFO)
        handler = logging.FileHandler(path, encoding='utf-8')
        handler.setFormatter(logging.Formatter('10-27 10:20:30 %(levelname)s %(message)s'))
        logger.addHandler(handler)
        try:
            for name in ('alpha', 'beta'):
                InstanceLog(logger, dict(instance=name)).info('接收判定 metadata=%s', json.dumps(dict(local_id=7, reason='accepted')))
        finally:
            logger.removeHandler(handler); handler.close()
        rows, error = safe_events(path, instance='alpha')
        self.assertFalse(error)
        self.assertEqual(len(rows), 1)
        self.assertIn('instance=alpha', rows[0])
        self.assertIn('reason=accepted', rows[0])

    def test_release_does_not_ignore_disabled_queue_or_unconfirmed_account(self):
        self.create_queues()
        self.instance_data[0]['enabled'] = False
        self.load()
        value = deploy.queue()
        self.assertEqual(value['memory_pending'], 2)
        self.assertEqual(len(value['identity']), 2)
        self.assertFalse(deploy.listener_idle(dict(pending=0, memory_pending=0), dict(send_blocked=True)))

    def test_database_check_gets_exclusive_owner_before_creating_reader(self):
        from wxbridge.app.diagnostics import check
        from wechat_linux.errors import OwnerBusyError
        with patch('wxbridge.app.runtime.instance_lock', side_effect=OwnerBusyError('resource_busy')), \
             patch('wxbridge.app.diagnostics.WeChatStore') as store:
            with self.assertRaises(OwnerBusyError): check(self.cfg['instances'][0]['config'])
            store.assert_not_called()


if __name__ == '__main__': unittest.main()
