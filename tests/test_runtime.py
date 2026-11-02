"""Foreground startup preserves desired snapshots and explicitly selects send mode."""
from contextlib import nullcontext
from dataclasses import replace
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from tests.support.model_support import default_config
from wxbridge.config import AppConfig, FrozenMap, RuntimePaths, RuntimePolicy
from wxbridge.app.runtime import run_bot
from wxbridge_cli.debug import main


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        root=Path(__file__).resolve().parents[1]/'.test-tmp'
        self.temp=tempfile.TemporaryDirectory(dir=root);self.addCleanup(self.temp.cleanup)
        directory=Path(self.temp.name);base=default_config()
        account=replace(base.account,state_file=str(directory/'state.json'),queue_file=str(directory/'queue.sqlite3'),
                        decrypted_dir=str(directory/'decrypted'),log_file=str(directory/'bot.log'))
        instance=replace(base,id='synthetic',chat='synthetic@chatroom',account=account,
                         paths=RuntimePaths(account.queue_file,account.state_file))
        self.cfg=AppConfig(account=account,instances=FrozenMap((('synthetic',instance),)))
        lock=patch('wxbridge.app.runtime.instance_lock',side_effect=lambda path:nullcontext())
        lock.start();self.addCleanup(lock.stop)

    def test_baseline_before_poll_and_no_config_mutation(self):
        self.cfg=replace(self.cfg,runtime=RuntimePolicy(reply_to_backlog=True));original=self.cfg
        bot,order=Mock(),Mock()
        for name in ('save_status','run','close'):order.attach_mock(getattr(bot,name),name)
        with patch('wxbridge.app.runtime.setup_logging'),patch('wxbridge.app.sessions.coordinator.Coordinator',return_value=bot) as factory:
            self.assertEqual(run_bot(self.cfg),0)
        self.assertIs(self.cfg,original)
        self.assertEqual([item[0] for item in order.mock_calls],['save_status','run','close'])
        active=factory.call_args.args[0]
        self.assertTrue(active.paths.queue_file.endswith('.dry-run'))
        self.assertFalse(active.runtime.reply_to_backlog)
        self.assertEqual(active.instances['synthetic'].participation,original.instances['synthetic'].participation)

    def test_status_marks_stopped_even_when_loop_raises(self):
        bot=Mock();bot.run.side_effect=RuntimeError('synthetic failure')
        with patch('wxbridge.app.runtime.setup_logging'),patch('wxbridge.app.sessions.coordinator.Coordinator',return_value=bot):
            with self.assertRaises(RuntimeError):run_bot(self.cfg)
        bot.close.assert_called_once()

    def test_startup_preserves_each_instances_participation_settings(self):
        first=self.cfg.instances['synthetic']
        second=replace(first,id='other',chat='other@chatroom',participation=replace(first.participation,enabled=False,pat_enabled=False))
        self.cfg=replace(self.cfg,instances=FrozenMap((('synthetic',first),('other',second))))
        with patch('wxbridge.app.runtime.setup_logging'),patch('wxbridge.app.sessions.coordinator.Coordinator') as factory:
            self.assertEqual(run_bot(self.cfg),0)
        active=factory.call_args.args[0]
        self.assertTrue(active.instances['synthetic'].participation.enabled)
        self.assertFalse(active.instances['other'].participation.enabled)
        self.assertFalse(active.instances['other'].participation.pat_enabled)
        self.assertEqual(self.cfg.instances['other'],second)

    def test_cli_send_and_dry_run_are_explicit_and_keep_model(self):
        for args,expected in ((['run'],True),(['run','--dry-run'],True),(['run','--send'],False)):
            with self.subTest(args=args),patch('wxbridge.config.load_config',return_value=self.cfg),patch('wxbridge.app.runtime.run_bot',return_value=0) as run:
                self.assertEqual(main(args),0)
                active=run.call_args.args[0]
                self.assertIs(active.account.ui.dry_run,expected)
                self.assertEqual(active.instances['synthetic'].models.reply.model,self.cfg.instances['synthetic'].models.reply.model)
