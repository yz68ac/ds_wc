"""Lifecycle tests with fake child processes; no live services or accounts."""
import json
import os
from pathlib import Path
import signal
from types import SimpleNamespace
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from wxbridge.host.supervisor import Child, Supervisor
from wxbridge.control import BOT, COMPONENTS, HELPER, RECOVERY, SESSION, WEB, receive, request


class ServiceEntryTests(unittest.TestCase):
    def test_runtime_preparation_is_an_isolated_process(self):
        from wxbridge import __main__ as entry
        with patch.object(entry.os,'geteuid',return_value=0,create=True), \
                patch.object(entry.subprocess,'run') as run, \
                patch('wxbridge.host.supervisor.Supervisor') as supervisor, \
                patch.object(entry.signal,'signal'):
            self.assertEqual(entry.main(['--root',str(Path.cwd())]),0)
        run.assert_called_once_with(['/usr/bin/python3',str(Path.cwd()/'scripts/boot-recovery.py'),'runtime'],check=True)
        supervisor.return_value.run.assert_called_once()

    @unittest.skipUnless(sys.platform=='linux','Linux system Python recovery process')
    def test_real_entry_does_not_keep_the_recovery_scanner_package_in_sys_modules(self):
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=root/'.test-tmp') as directory:
            target=Path(directory);(target/'scripts').mkdir()
            scanner=target/'scanner/wxbridge/storage';scanner.mkdir(parents=True)
            for path in (scanner.parent/'__init__.py',scanner/'__init__.py',scanner/'memoryfs.py'):
                path.write_text('')
            (target/'scripts/boot-recovery.py').write_text(
                'import sys\nfrom pathlib import Path\n'
                'def runtime():\n'
                ' sys.path.insert(0,str(Path(__file__).parents[1]/"scanner"))\n'
                ' import wxbridge.storage.memoryfs\n'
                'if __name__=="__main__":runtime()\n')
            code='''
import sys,types
sys.path.insert(0,sys.argv[1])
from wxbridge import __main__ as entry
module=types.ModuleType('wxbridge.host.supervisor')
class Supervisor:
 def __init__(self,*args,**kwargs):
  from wxbridge.control import request
 def run(self):print('entry_imports_complete_package')
module.Supervisor=Supervisor
sys.modules['wxbridge.host.supervisor']=module
entry.os.geteuid=lambda:0
entry.signal.signal=lambda *args:None
entry.main(['--root',sys.argv[2]])
'''
            result=subprocess.run([sys.executable,'-I','-c',code,str(root/'src'),str(target)],
                                  capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('entry_imports_complete_package',result.stdout)


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
        s.web_config=self.directory/'web.json'
        s.web_config.write_text(json.dumps(dict(host='127.0.0.1',port=8730)))
        s.web_config.chmod(0o600)
        s.web_saved=dict(host='127.0.0.1',port=8730)
        s.web_effective=None
        s.web_enabled=True
        s.web_ready=False
        s.web_generation,s.web_applied_generation=0,-1
        s.web_error=''
        s.web_health_token='synthetic-start-token'
        s.probe_web=Mock()
        self.web_reader=patch('wxbridge.host.supervisor.read_web',return_value=dict(s.web_saved))
        self.web_reader.start();self.addCleanup(self.web_reader.stop)
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

    @unittest.skipUnless(sys.platform=='linux','Linux Unix socket and filesystem modes')
    def test_bind_restores_group_access_after_systemd_reapplies_private_mode(self):
        s=self.supervisor;s.control_path=self.directory/'control.sock'
        s.account=SimpleNamespace(pw_gid=os.getgid())
        s.socket=None;s.lock_fd=None
        self.directory.chmod(0o700)
        original=Path.stat
        def metadata(path,*args,**kwargs):
            value=original(path,*args,**kwargs)
            return SimpleNamespace(st_uid=0,st_mode=value.st_mode) if path==self.directory else value
        try:
            with patch.object(Path,'stat',metadata),patch('wxbridge.host.supervisor.os.chown') as chown:
                s.bind()
            self.assertEqual(self.directory.stat().st_mode & 0o777,0o750)
            self.assertEqual(s.control_path.stat().st_mode & 0o777,0o660)
            chown.assert_any_call(self.directory,0,os.getgid())
        finally:
            if s.socket:s.socket.close()
            if s.lock_fd is not None:os.close(s.lock_fd)

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

    def test_web_stop_is_persistent_and_does_not_stop_other_components(self):
        self.desktop();s=self.supervisor;s.children[BOT].process=object()
        s.action('web_stop');s.tick()
        state=json.loads(s.state_file.read_text())
        self.assertEqual(state,dict(schema=2,bot_enabled=True,web_enabled=False))
        self.assertTrue(s.children[WEB].stopping)
        for name in (SESSION,HELPER,BOT):self.assertFalse(s.children[name].stopping)
        s.children[WEB].event=(0,None,True);s.tick();s.tick()
        self.assertNotIn(WEB,self.started)

    def test_web_restart_waits_for_old_process_exit(self):
        self.desktop();s=self.supervisor
        s.action('web_restart');s.tick()
        self.assertNotIn(WEB,self.started)
        self.assertTrue(s.children[WEB].stopping)
        s.children[WEB].event=(0,None,True);s.tick()
        self.assertEqual(self.started.count(WEB),1)
        self.assertTrue(s.enabled)

    def test_web_reload_preserves_disabled_switch_and_resets_readiness(self):
        s=self.supervisor;s.web_enabled=False;s.web_ready=True
        s.action('web_reload');s.tick()
        self.assertFalse(s.web_enabled);self.assertFalse(s.web_ready)
        self.assertNotIn(WEB,self.started)

    def test_web_crash_is_recovered_only_when_enabled(self):
        self.desktop();s=self.supervisor
        s.children[WEB].event=(1,None,False);s.tick()
        self.assertIn(WEB,self.started)
        self.assertEqual(s.web_error,'web_process_exited')
        s.action('web_stop');s.children[WEB].event=(0,None,True)
        self.started.clear();s.tick()
        self.assertNotIn(WEB,self.started)

    def test_service_user_cannot_control_web_but_can_control_bot(self):
        s=self.supervisor
        for action in ('web_start','web_stop','web_restart','web_reload'):
            with self.assertRaisesRegex(ValueError,'requires_root'):s.action(action,uid=995)
        self.assertFalse(s.state_file.exists())
        s.action('stop',uid=995)
        self.assertFalse(s.enabled)

    def test_health_probe_uses_no_proxy_and_only_marks_running_generation(self):
        s=self.supervisor;self.desktop()
        s.web_next_probe=0;s.web_launch_settings=dict(s.web_saved)
        response=Mock();response.status=200;response.read.return_value=b'{"status":"ok"}'
        response.headers={'X-WXBridge-Health':s.web_health_token}
        response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        opener=Mock();opener.open.return_value=response
        with patch('wxbridge.host.supervisor.urllib.request.build_opener',return_value=opener), \
                patch('wxbridge.host.supervisor.urllib.request.ProxyHandler') as proxy:
            Supervisor.probe_web(s)
        proxy.assert_called_once_with({})
        self.assertTrue(s.web_ready);self.assertEqual(s.web_effective,s.web_saved)
        self.assertEqual(s.web_applied_generation,s.web_generation)
        s.children[WEB].stopping=True;s.web_generation+=1;s.web_next_probe=0
        with patch('wxbridge.host.supervisor.urllib.request.build_opener') as probe:
            Supervisor.probe_web(s)
        probe.assert_not_called()

    def test_other_process_health_response_cannot_confirm_this_web_process(self):
        s=self.supervisor;self.desktop()
        s.web_next_probe=0;s.web_launch_settings=dict(s.web_saved)
        response=Mock();response.status=200;response.read.return_value=b'{"status":"ok"}'
        response.headers={'X-WXBridge-Health':'another-process'}
        response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        opener=Mock();opener.open.return_value=response
        with patch('wxbridge.host.supervisor.urllib.request.build_opener',return_value=opener):
            Supervisor.probe_web(s)
        self.assertFalse(s.web_ready);self.assertIsNone(s.web_effective)

    def test_failed_health_probe_does_not_report_ready(self):
        s=self.supervisor;self.desktop()
        s.web_next_probe=0;s.web_launch_settings=dict(s.web_saved)
        with patch('wxbridge.host.supervisor.urllib.request.build_opener',side_effect=OSError()):
            Supervisor.probe_web(s)
        self.assertFalse(s.web_ready);self.assertIsNone(s.web_effective)

    def test_process_credentials_are_dropped_at_spawn(self):
        child=Child(BOT,['python','fixed-script.py'],uid=995,gid=984,groups=[984],cwd='/trusted')
        process=Mock();process.poll.return_value=None
        with patch('wxbridge.host.supervisor.subprocess.Popen',return_value=process) as spawn:
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
             patch('wxbridge.host.supervisor.os.getpid',return_value=123):
            self.assertEqual(Supervisor.remaining_processes(),[456])
        with patch('pathlib.Path.read_text',return_value='0::/system.slice/other.service\n'):
            with self.assertRaisesRegex(ValueError,'own_systemd_cgroup'):
                Supervisor.remaining_processes()


if __name__=='__main__':unittest.main()
