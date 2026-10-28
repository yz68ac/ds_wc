"""合成状态与终端：只读、过期提示、脱敏、导航和Linux真实PTY退出恢复。"""
from model_support import default_config
from contextlib import nullcontext
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import Mock, patch

from wxbridge.app import monitor, terminal
from wxbridge.cli import main
from wxbridge.config import DEFAULTS

NOW = 1700000600


def live():
    return dict(phase='generating', pid=123, updated_at=NOW, started_at=NOW-60, queue_length=1,
        generation_workers=1, cursor=9, last_success=NOW-8, dry_run=False, blocked_reason='',
        build=dict(commit='a'*40), memory_pending=2, memory_error='',
        memory=dict(enabled=True, running=False, members=3, entries=7, revision=4, watermark=6,
                    initial_impressions=1, learning_candidates=2, learning_stable=1),
        proactive=dict(enabled=True, outcome='generating_reply', model_role='reply', evaluations=2),
        conversation=dict(active=True, expires_at=NOW+120, followups_sent=2),
        tasks=[dict(id=1, version=1, phase='queued', generation='running', trigger_kind='mention',
                    next_chunk=0, chunks=1, generation_attempts=1, text='PRIVATE_CHAT')],
        recent_intake=[dict(local_id=9, reason='accepted', text='PRIVATE_CHAT')],
        metrics=dict(models={'reply':dict(calls=2, errors=1, prompt_tokens=100, completion_tokens=20, elapsed_ms=1500)},
                     stages={'intake':dict(count=4,last_ms=.5,max_ms=1)}),
        api_key='sk-'+'a'*32, body='PRIVATE_CHAT')


def snapshot():
    return dict(live=live(), boot=dict(phase='listening'), services={monitor.UNITS[0]:dict(
        ActiveState='active', SubState='running', MainPID='123', NRestarts='0')},
        file_error='', service_error='', events=[], log_error='', collected_at=NOW)


class MonitorTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1]/'.test-tmp'
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = default_config()
        self.cfg.update(state_file=str(self.root/'state.json'), log_file=str(self.root/'bot.log'))
        self.path = self.root/'live-status.json'
        self.path.write_text(json.dumps(live()), encoding='utf-8')
        (self.root/'bot.log').write_text('10-22 01:02:03 INFO 开始生成 task=1 version=1\n', encoding='utf-8')

    def test_cli_once_reads_only_snapshots_and_never_starts_model_store_or_queue(self):
        before = {p.name:(p.read_bytes(),p.stat().st_mtime_ns) for p in self.root.iterdir()}
        with patch('wxbridge.cli.load_config', return_value=self.cfg), patch.object(monitor,'read_services',return_value=({},'')), \
                patch('wxbridge.app.bot.WeChatStore') as store, patch('wxbridge.ai.client.client_for') as client, \
                patch('sqlite3.connect',side_effect=AssertionError('must not query databases')), \
                patch('sys.stdout',new_callable=io.StringIO) as output:
            self.assertEqual(main(['status','--once']),0)
        self.assertIn('概览',output.getvalue())
        self.assertIn('模型用量',output.getvalue())
        self.assertNotIn('PRIVATE_CHAT',output.getvalue())
        self.assertNotIn('sk-'+'a'*32,output.getvalue())
        store.assert_not_called(); client.assert_not_called()
        self.assertEqual(before,{p.name:(p.read_bytes(),p.stat().st_mtime_ns) for p in self.root.iterdir()})

    def test_pages_show_generation_usage_memory_and_window_without_unselected_data(self):
        view = monitor.pages(snapshot(),self.cfg)
        text='\n'.join(line for page in view for line in page)
        configured = monitor.number(monitor.model_settings(self.cfg,'memory')['max_tokens'])
        for expected in ('心跳正常','生成回复','123','120 秒','记忆待整理：2','100','关闭思考',configured):
            self.assertIn(expected,text)
        self.assertNotIn('PRIVATE_CHAT',text)
        self.assertNotIn('sk-'+'a'*32,text)
        self.assertEqual(len(view),5)

    def test_health_distinguishes_stale_stopped_future_and_replaced_process(self):
        view=snapshot()
        self.assertIn('过期',monitor.health(view,NOW+11))
        self.assertIn('时钟',monitor.health(view,NOW-6))
        view['live']['phase']='stopped'
        self.assertIn('已停止',monitor.health(view,NOW))
        view['live']['phase']='listening'
        view['services'][monitor.UNITS[0]]['MainPID']='456'
        self.assertIn('进程已变化',monitor.health(view,NOW))
        view['services'][monitor.UNITS[0]]['ActiveState']='inactive'
        self.assertIn('服务未运行',monitor.health(view,NOW))

    def test_missing_invalid_large_or_non_object_status_remains_readable(self):
        self.path.unlink()
        self.assertEqual(monitor.read_json(self.path)[1],'missing')
        for body in ('broken','[]','{"x":'*2000+'0'):
            self.path.write_text(body)
            self.assertEqual(monitor.read_json(self.path)[1],'invalid')
        with patch.object(monitor.json,'loads',side_effect=RecursionError):
            self.assertEqual(monitor.read_json(self.path)[1],'invalid')
        self.path.write_bytes(b' '* (2*1024*1024+1))
        self.assertEqual(monitor.read_json(self.path)[1],'large')
        view=snapshot();view.update(live={},file_error='denied',services={})
        self.assertIn('权限不足','\n'.join(monitor.pages(view,self.cfg)[0]))

    @unittest.skipIf(os.name=='nt','Linux FIFO和符号链接读取保护')
    def test_non_regular_or_symlink_snapshot_cannot_block_or_follow_target(self):
        link=self.root/'linked.json'; link.symlink_to(self.path)
        self.assertTrue(monitor.read_json(link)[1])
        fifo=self.root/'fifo'; os.mkfifo(fifo)
        began=time.monotonic()
        self.assertEqual(monitor.read_json(fifo)[1],'invalid')
        self.assertLess(time.monotonic()-began,.5)

    def test_service_failure_is_bounded_and_does_not_forward_credentials(self):
        with patch.object(monitor.subprocess,'run',side_effect=subprocess.TimeoutExpired('systemctl',2)) as run:
            self.assertEqual(monitor.read_services(),({},'unavailable'))
        self.assertEqual(run.call_args.kwargs['timeout'],2)
        self.assertEqual(set(run.call_args.kwargs['env']),{'PATH','LC_ALL'})
        self.assertEqual(run.call_args.args[0][1],'show')

    def test_systemd_parses_fixed_units_only(self):
        body='Id=wxbridge-bot.service\nActiveState=active\nMainPID=123\n\nId=unrelated.service\nActiveState=active\n'
        with patch.object(monitor.subprocess,'run',return_value=Mock(returncode=0,stdout=body)):
            services,failure=monitor.read_services()
        self.assertFalse(failure)
        self.assertEqual(set(services),{'wxbridge-bot.service'})

    def test_details_cache_and_manual_refresh_do_not_poll_systemd_every_tick(self):
        with patch.object(monitor,'read_services',return_value=({},'')) as service, \
                patch.object(monitor,'safe_events',return_value=([],'')) as logs, \
                patch.object(monitor.time,'monotonic',side_effect=[0,1,2,7]):
            reader=monitor.StatusReader(self.cfg)
            reader.read();reader.read();reader.read(force=True);reader.read()
        self.assertEqual(service.call_count,3)
        self.assertEqual(logs.call_count,3)

    def test_events_only_project_known_fields_and_mask_key_like_error_codes(self):
        secret='sk-'+'z'*32
        body=('10-22 01:02:03 INFO 开始生成 task=1 text=PRIVATE_CHAT '+secret+'\x1b[2J\n'
              '10-22 01:02:04 ERROR 未知日志 PRIVATE_CHAT\n'
              'Traceback PRIVATE_CHAT\n'
              '10-22 01:02:05 WARNING 记忆延后 category='+secret+'\n'
              '10-22 01:02:06 INFO 发送操作 task=1 part=0 metadata={"error":"no_outbound_record","body":"PRIVATE_CHAT"}\n')
        path=Path(self.cfg['log_file']);path.write_text(body,encoding='utf-8')
        events,failure=monitor.safe_events(path)
        text='\n'.join(events)
        self.assertFalse(failure)
        self.assertEqual(len(events),3)
        self.assertIn('no_outbound_record',text)
        self.assertNotIn('PRIVATE_CHAT',text)
        self.assertNotIn(secret,text)
        self.assertNotIn('\x1b',text)

    def test_malformed_optional_values_never_turn_into_raw_strings(self):
        view=snapshot()
        view['live'].update(queue_length={'secret':'PRIVATE_CHAT'},blocked_reason='私人聊天正文',pid=True)
        view['live']['memory']['members']=float('nan')
        text='\n'.join(monitor.pages(view,self.cfg)[0])
        self.assertNotIn('PRIVATE_CHAT',text)
        self.assertNotIn('私人聊天正文',text)
        self.assertNotIn('nan',text)

    def test_interval_rejects_nan_infinity_or_out_of_bounds_before_opening_panel(self):
        for value in ('nan','inf','0.1','31'):
            with self.subTest(value=value), patch('wxbridge.cli.load_config',return_value=self.cfg), \
                    patch.object(terminal,'run_status') as run,patch('sys.stdout',new_callable=io.StringIO):
                self.assertEqual(main(['status','--interval',value]),1)
                run.assert_not_called()

    def test_non_tty_has_actionable_exit_and_does_not_start_polling(self):
        with patch('sys.stdin.isatty',return_value=False),patch('sys.stderr',new_callable=io.StringIO) as output, \
                patch.object(monitor.StatusReader,'read') as read:
            self.assertEqual(terminal.run_status(self.cfg),2)
        read.assert_not_called()
        self.assertIn('ssh -t',output.getvalue())
        self.assertIn('--once',output.getvalue())

    def test_unicode_clipping_and_wrapping_preserve_long_lines_without_controls(self):
        self.assertEqual(terminal.clip('中文ab',5),'中文a')
        self.assertEqual(terminal.clip('e\u0301中',2),'e\u0301')
        self.assertEqual(terminal.wrap_lines(['中文abc'],4),['中文','abc'])
        self.assertNotIn('\x1b',terminal.clip('x\x1by',10))

    def test_navigation_pause_refresh_help_resize_and_quit(self):
        class CursesError(Exception):
            pass
        constants={name:i+1000 for i,name in enumerate(('KEY_RIGHT','KEY_LEFT','KEY_BTAB','KEY_UP','KEY_DOWN','KEY_PPAGE','KEY_NPAGE','KEY_HOME','KEY_END'))}
        curses=types.SimpleNamespace(**constants,error=CursesError,A_BOLD=1,curs_set=Mock())
        screen=Mock()
        screen.getmaxyx.side_effect=[(8,25),(24,80)]+[(24,80)]*20
        screen.get_wch.side_effect=['2','j','\t',' ','r','?','h','5','G','q']
        reader=Mock(cfg=self.cfg);reader.read.return_value=snapshot()
        with patch.dict(sys.modules,{'curses':curses}):
            self.assertEqual(terminal.screen_loop(screen,reader,1),0)
        self.assertTrue(any(call.kwargs.get('force') for call in reader.read.call_args_list))
        text='\n'.join(c.args[2] if len(c.args)>2 else '' for c in screen.addstr.call_args_list)
        self.assertIn('已暂停采集',text)
        self.assertIn('键盘操作',text)

    @unittest.skipIf(os.name=='nt','真实curses/PTY仅在Linux验证')
    def test_real_linux_pty_keys_resize_exit_restore_terminal_without_live_services(self):
        import fcntl
        import pty
        import select
        import signal
        import struct
        import termios
        config=self.root/'config.json'
        config.write_text(json.dumps(dict(state_file=self.cfg['state_file'],log_file=self.cfg['log_file'])),encoding='utf-8')
        master,slave=pty.openpty()
        saved=termios.tcgetattr(slave)
        fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',24,100,0,0))
        bootstrap=("import sys; from wxbridge.app import monitor; monitor.read_services=lambda: ({},'unavailable'); "
                   "from wxbridge.cli import main; raise SystemExit(main(['--config',sys.argv[1],'status']))")
        env=dict(os.environ,TERM='xterm-256color',LANG='C.UTF-8')
        child=subprocess.Popen([sys.executable,'-c',bootstrap,str(config)],cwd=Path(__file__).resolve().parents[1],
                               stdin=slave,stdout=slave,stderr=slave,env=env)
        captured=b''; sent=False; deadline=time.monotonic()+8
        try:
            while time.monotonic()<deadline:
                if select.select([master],[],[],.1)[0]:
                    try:
                        captured+=os.read(master,65536)
                    except OSError:
                        break
                if not sent and '运行面板'.encode() in captured:
                    fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',14,60,0,0))
                    child.send_signal(signal.SIGWINCH)
                    os.write(master,b'2345\t r?hq')
                    sent=True
                if child.poll() is not None:
                    break
            self.assertTrue(sent,'terminal did not render')
            self.assertEqual(child.wait(timeout=2),0)
            mask=termios.ECHO|termios.ICANON
            self.assertEqual(termios.tcgetattr(slave)[3]&mask,saved[3]&mask)
            self.assertNotIn(b'PRIVATE_CHAT',captured)
            self.assertNotIn(b'Traceback',captured)
        finally:
            if child.poll() is None:
                child.kill();child.wait(timeout=2)
            os.close(master);os.close(slave)
