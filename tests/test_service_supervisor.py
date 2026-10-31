"""Lifecycle tests with fake child processes; no live services or accounts."""
import json
from pathlib import Path
import signal
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from wxbridge_service.supervisor import Child, Supervisor
from wxbridge_web.control import BOT, COMPONENTS, HELPER, RECOVERY, SESSION, WEB, receive, request


class FakeChild:
    def __init__(self,name,alive=False):
        self.name,self.process,self.stopping,self.event=name,object() if alive else None,False,None
        self.stops=[]

    @property
    def running(self):return self.process is not None
    def stop(self,sig=signal.SIGTERM):
        self.stops.append(sig)
        if self.running:self.stopping=True
    def reap(self):
        event,self.event=self.event,None
        if event is not None:self.process=None;self.stopping=False
        return event
    def status(self):return dict(state='active' if self.running else 'inactive',pid=123 if self.running else 0)


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        parent=Path(__file__).resolve().parents[1]/'.test-tmp';parent.mkdir(exist_ok=True)
        temp=tempfile.TemporaryDirectory(dir=parent);self.addCleanup(temp.cleanup)
        self.directory=Path(temp.name)
        s=self.supervisor=Supervisor.__new__(Supervisor)
        s.state_file=self.directory/'state.json'
        s.run_dir=self.directory
        s.enabled,s.stopping,s.ready,s.checkpoint_needed=True,False,False,False
        s.phase,s.error,s.next_prepare='starting','',0
        s.children={n:FakeChild(n) for n in (*COMPONENTS,'checkpoint')}
        self.started=[]
        def start(name,**kwargs):
            child=s.children[name]
            if child.process is not None:return False
            self.started.append(name);child.process=object();return True
        s.start=start

    def desktop(self):
        (self.directory/'env').write_text('DISPLAY=:99')
        (self.directory/'wechat.pid').write_text('123')
        for name in (SESSION,HELPER,WEB):self.supervisor.children[name].process=object()

    def test_stop_persists_pause_and_preserves_desktop_and_web(self):
        self.desktop();s=self.supervisor;s.children[BOT].process=object()
        s.action('stop');s.tick()
        self.assertFalse(json.loads(s.state_file.read_text())['bot_enabled'])
        self.assertTrue(s.children[BOT].stopping)
        self.assertEqual(s.children[BOT].stops[0],signal.SIGINT)
        self.assertFalse(s.children[SESSION].stopping)
        self.assertFalse(s.children[WEB].stopping)
        self.assertNotIn(RECOVERY,self.started)

    def test_apply_does_not_resume_a_paused_bot(self):
        s=self.supervisor;s.enabled=False
        self.assertFalse(s.action('apply')['applied'])
        self.assertFalse(s.enabled);self.assertFalse(s.state_file.exists())

    def test_bot_exit_checkpoints_before_recovery_or_restart(self):
        self.desktop();s=self.supervisor
        s.children[BOT].event=(0,None,False)
        s.tick()
        self.assertEqual(self.started,['checkpoint'])
        s.children['checkpoint'].event=(0,b'{}',False)
        s.tick()
        self.assertEqual(self.started,['checkpoint',RECOVERY])
        s.children[RECOVERY].event=(0,b'{"ready":true}',False)
        s.tick()
        self.assertEqual(self.started,['checkpoint',RECOVERY,BOT])

    def test_failed_preparation_waits_and_leaves_web_usable(self):
        self.desktop();s=self.supervisor
        s.children[RECOVERY].event=(1,b'{"reason":"waiting_for_wechat_login"}',False)
        s.tick()
        self.assertFalse(s.ready);self.assertNotIn(BOT,self.started)
        self.assertFalse(s.children[WEB].stopping)
        self.assertEqual(s.error,'waiting_for_login_or_repair')

    def test_session_failure_drains_bot_and_helper_before_restart(self):
        self.desktop();s=self.supervisor;s.children[BOT].process=object()
        s.children[SESSION].event=(1,None,False)
        s.tick()
        self.assertTrue(s.children[BOT].stopping)
        self.assertTrue(s.children[HELPER].stopping)
        self.assertNotIn(SESSION,self.started)
        self.assertFalse(s.children[WEB].stopping)

    def test_shutdown_waits_for_bot_and_checkpoint_before_desktop(self):
        self.desktop();s=self.supervisor;s.children[BOT].process=object();s.stopping=True
        s.tick();self.assertFalse(s.children[SESSION].stopping)
        s.children[BOT].event=(0,None,True);s.tick()
        self.assertIn('checkpoint',self.started);self.assertFalse(s.children[SESSION].stopping)
        s.children['checkpoint'].event=(0,b'{}',False);s.tick()
        self.assertTrue(s.children[HELPER].stopping);self.assertFalse(s.children[SESSION].stopping)
        s.children[HELPER].event=(0,None,True);s.tick()
        self.assertTrue(s.children[SESSION].stopping)

    def test_fixed_actions_reject_shell_commands(self):
        with self.assertRaises(ValueError):self.supervisor.action('stop sshd.service')
        self.assertFalse(self.supervisor.state_file.exists())

    def test_process_credentials_are_dropped_at_spawn(self):
        child=Child(BOT,['python','fixed-script.py'],uid=995,gid=984,groups=[984],cwd='/trusted')
        process=Mock();process.poll.return_value=None
        with patch('wxbridge_service.supervisor.subprocess.Popen',return_value=process) as spawn:
            self.assertTrue(child.start({'PATH':'/usr/bin'},capture=True))
        self.assertEqual(spawn.call_args.kwargs['user'],995)
        self.assertEqual(spawn.call_args.kwargs['group'],984)
        self.assertTrue(spawn.call_args.kwargs['start_new_session'])
        self.assertNotIn('shell',spawn.call_args.kwargs)

    def test_helper_shutdown_has_no_force_kill_deadline(self):
        child=Child(HELPER,['fixed-helper'],timeout=None)
        child.process=Mock();child.process.pid=123;child.process.poll.return_value=None
        with patch.object(child,'_signal') as send:
            child.stop();child.reap()
        self.assertEqual(child.deadline,float('inf'))
        send.assert_called_once_with(signal.SIGTERM)

    def test_protocol_rejects_extra_and_oversized_frames(self):
        for raw in (b'{"action":"status"}\n{}\n', b'x'*8193):
            stream=Mock();stream.recv.return_value=raw
            with self.assertRaises(ValueError):receive(stream)
        with self.assertRaises(ValueError):request('arbitrary-command')

    def test_desktop_daemon_cleanup_excludes_supervisor_and_other_cgroups(self):
        with patch('pathlib.Path.read_text',side_effect=['0::/system.slice/wxbridge.service\n','123\n456\n']),\
             patch('wxbridge_service.supervisor.os.getpid',return_value=123):
            self.assertEqual(Supervisor.remaining_processes(),[456])
        with patch('pathlib.Path.read_text',return_value='0::/system.slice/other.service\n'):
            with self.assertRaisesRegex(ValueError,'own_systemd_cgroup'):
                Supervisor.remaining_processes()


if __name__=='__main__':unittest.main()
