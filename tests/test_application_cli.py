"""Public command routing and explicit worker sending; no live service actions."""
from contextlib import redirect_stdout
import io
from pathlib import Path
import unittest
from unittest.mock import patch

from wxbridge.cli import main
from wxbridge_service import cli
from wxbridge.app import runtime


class ApplicationCLITests(unittest.TestCase):
    def test_public_entry_is_the_host_control_cli(self):
        self.assertIs(main,cli.main)
        with patch.object(cli,'current_status',return_value=dict(phase='synthetic',instances={})), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(['status','--json']),0)
        self.assertIn('synthetic',output.getvalue())

    def test_debug_uses_selected_root_and_never_dispatches_service_control(self):
        root=Path(__file__).resolve().parents[1]/'.test-tmp/synthetic-app-root'
        with patch('wxbridge.debug_cli.execute',return_value=0) as debug, patch.object(cli,'control_request') as control:
            self.assertEqual(main(['--root',str(root),'debug','--instance','synthetic','queue-status']),0)
        args=debug.call_args.args[0]
        self.assertEqual(args.command,'queue-status')
        self.assertEqual(args.config,str(root/'config.json'))
        self.assertEqual(args.instance,'synthetic')
        control.assert_not_called()

    def test_explicit_debug_config_overrides_root_default(self):
        with patch('wxbridge.debug_cli.execute',return_value=0) as debug:
            self.assertEqual(main(['--root','/synthetic','debug','--config','custom.json','explain-config']),0)
        self.assertEqual(debug.call_args.args[0].config,'custom.json')

    def test_internal_worker_requires_explicit_send_flag(self):
        for flags,sending in (([],False),(['--send'],True)):
            cfg=dict(ui=dict(dry_run=False))
            with self.subTest(flags=flags), patch('wxbridge.config.load_config',return_value=cfg), patch.object(runtime,'run_bot',return_value=0) as run:
                self.assertEqual(runtime.main(['--config','synthetic.json',*flags]),0)
                self.assertEqual(run.call_args.args[0]['ui']['dry_run'],not sending)


if __name__=='__main__':
    unittest.main()
