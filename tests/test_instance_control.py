"""Owner-loop lifecycle with detached fake sessions; no WeChat DB, model or UI calls."""
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
from threading import Event
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock,patch

from wxbridge.config import load_config
from wxbridge.app.instance_control import InstanceControl
from wxbridge.app.coordinator import Coordinator
from wxbridge.app.model_runtime import ModelLane
from wxbridge_web.demo import seed
from wxbridge_web.repository import ConfigRepository


class FakeSession:
    def __init__(self,cfg,*,instance_id,**kwargs):
        self.cfg,self.instance_id=cfg,instance_id
        self.quiescing,self.ready,self.closed=False,False,False
        self.chat_id=cfg['target']['chat']
        self.calls=[]
    def begin_stop(self):self.quiescing=True
    def drain_stop(self):return self.ready
    def close(self):self.closed=True
    def intake(self):self.calls.append('intake')
    def collect_results(self):self.calls.append('collect')
    def prioritize(self):self.calls.append('prioritize')
    def start_foreground(self):self.calls.append('generate')
    def participate(self):self.calls.append('participate')
    def deliver(self):self.calls.append('deliver')
    def background(self):self.calls.append('background')
    def maintain(self):self.calls.append('maintain')


class InstanceControlTests(unittest.TestCase):
    def setUp(self):
        parent=Path(__file__).resolve().parents[1]/'.test-tmp';parent.mkdir(exist_ok=True)
        temporary=tempfile.TemporaryDirectory(dir=parent);self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);seed(self.root)
        self.repo=ConfigRepository(self.root)
        group=self.repo.get('group_demo');group['config']['enabled']=True
        self.repo.save('group_demo',group['config'],group['revision'])
        cfg=load_config(self.root/'config.json')
        cfg['ui']['dry_run']=True
        sessions=[FakeSession(entry['config'],instance_id=entry['id']) for entry in cfg['instances'] if entry['enabled']]
        store=Mock(self_wxid='wxid_synthetic_bot');store.resolve_chat.side_effect=lambda value:(value,'synthetic')
        self.owner=SimpleNamespace(cfg=cfg,sessions=sessions,echo=False,account=SimpleNamespace(store=store,inactive=[]),build={})
        self.control=InstanceControl(self.owner);self.addCleanup(self.control.close)
        self.a=next(session for session in sessions if session.instance_id=='private_demo')
        self.b=next(session for session in sessions if session.instance_id=='group_demo')
        self.enterContext(patch.object(self.control,'_inactive'))
        self.enterContext(patch('wxbridge.app.runtime.instance_lock',side_effect=lambda value:nullcontext()))
        self.constructor=self.enterContext(patch('wxbridge.app.bot.GroupSession',side_effect=FakeSession))

    def request(self,name,action):
        item=self.repo.get(name)
        result=self.repo.request_instance(name,action,item['revision'])
        self.control.checked=0
        self.control.tick()
        return result

    def test_stopping_one_instance_keeps_others_and_waits_for_physical_end(self):
        self.request('private_demo','stop')
        self.assertTrue(self.a.quiescing);self.assertFalse(self.a.closed)
        self.assertFalse(self.b.quiescing)
        self.assertEqual(self.control.status()['private_demo']['state'],'stopping')
        self.a.ready=True;self.control.tick()
        self.assertTrue(self.a.closed);self.assertIn(self.b,self.owner.sessions)
        self.assertFalse(self.b.closed)
        self.assertEqual(self.control.status()['private_demo']['state'],'stopped')

    def test_model_apply_rebuilds_only_target_and_uses_new_model_snapshot(self):
        item=self.repo.get('private_demo');item['config']['models']['reply']['model']='replacement-model'
        self.repo.save('private_demo',item['config'],item['revision'])
        self.request('private_demo','apply')
        self.a.ready=True;self.control.tick()
        current=next(session for session in self.owner.sessions if session.instance_id=='private_demo')
        self.assertIsNot(current,self.a)
        self.assertEqual(current.cfg['models']['reply']['model'],'replacement-model')
        self.assertIs(next(session for session in self.owner.sessions if session.instance_id=='group_demo'),self.b)
        self.assertFalse(self.b.quiescing)

    def test_plain_save_does_not_implicitly_apply(self):
        item=self.repo.get('private_demo');item['config']['models']['reply']['model']='saved-draft'
        saved=self.repo.save('private_demo',item['config'],item['revision'])
        self.control.tick()
        self.assertFalse(self.a.quiescing)
        self.assertNotEqual(self.control.status()['private_demo']['applied_revision'],saved['revision'])

    def test_stale_request_rejected_before_touching_runtime(self):
        item=self.repo.get('private_demo');self.repo.request_instance('private_demo','stop',item['revision'])
        changed=self.repo.get('private_demo');changed['config']['models']['reply']['model']='changed-after-request'
        self.repo.save('private_demo',changed['config'],changed['revision'])
        self.control.checked=0;self.control.tick()
        self.assertFalse(self.a.quiescing)
        self.assertEqual(self.control.status()['private_demo']['error'],'instance_configuration_changed')

    def test_restart_does_not_replay_already_applied_request(self):
        item=self.repo.get('private_demo');self.repo.request_instance('private_demo','apply',item['revision'])
        restarted=InstanceControl(self.owner);self.addCleanup(restarted.close)
        restarted.tick()
        self.assertFalse(self.a.quiescing)

    def test_newest_request_wins_while_an_old_instance_is_stopping(self):
        self.request('private_demo','stop')
        self.request('private_demo','start')
        self.a.ready=True;self.control.tick()
        self.assertEqual(self.control.status()['private_demo']['state'],'running')
        self.assertTrue(self.repo.get('private_demo')['config']['enabled'])

    def test_all_instances_can_stop_without_account_tick_modulo_errors(self):
        self.request('private_demo','stop');self.a.ready=True;self.control.tick()
        self.request('group_demo','stop');self.b.ready=True;self.control.tick()
        self.assertEqual(self.owner.sessions,[])
        self.assertTrue(all(row['state']=='stopped' for row in self.control.status().values()))
        from wxbridge.app.coordinator import Coordinator
        coordinator=Coordinator.__new__(Coordinator)
        coordinator.sessions=[];coordinator.instance_control=None
        coordinator.rotation=coordinator.delivery_rotation=0;coordinator.native_api=None
        coordinator.save_status=Mock()
        coordinator.tick()
        coordinator.save_status.assert_called_once()

    def test_other_instance_completes_all_phases_during_drain(self):
        self.request('private_demo','stop')
        from wxbridge.app.coordinator import Coordinator
        coordinator=Coordinator.__new__(Coordinator)
        coordinator.sessions=self.owner.sessions;coordinator.instance_control=self.control
        coordinator.rotation=coordinator.delivery_rotation=0;coordinator.native_api=None
        coordinator.account=SimpleNamespace(sending=Mock(return_value=None))
        coordinator.save_status=Mock()
        coordinator.tick()
        self.assertEqual(self.a.calls,[])
        self.assertIn('intake',self.b.calls)
        self.assertIn('deliver',self.b.calls)
        self.assertIn('maintain',self.b.calls)


class NonblockingLaneStopTests(unittest.TestCase):
    def test_begin_close_returns_before_active_request_and_other_lane_runs(self):
        started,release,other_started=Event(),Event(),Event()
        lane=ModelLane('reply','synthetic',None)
        other=ModelLane('reply','synthetic',None)
        try:
            first=lane.submit(lambda job:(started.set(),release.wait(3),{})[-1],10)
            self.assertTrue(started.wait(2))
            queued=lane.submit(Mock(),10)
            lane.begin_close()
            self.assertFalse(first.future.done())
            self.assertTrue(queued.future.cancelled())
            other.submit(lambda job:other_started.set() or {},10)
            self.assertTrue(other_started.wait(2))
            self.assertFalse(lane.stopped())
        finally:
            release.set();lane.close();other.close()


class RealJournalTransitionTests(unittest.TestCase):
    def test_stop_start_keeps_cursor_queue_and_other_group_instance(self):
        import test_instances as fixtures
        fixture=fixtures.MultiGroupTests('test_one_reader_two_independent_cursors_contexts_and_model_slots')
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        fixture.add(0,1,'synthetic queued message')
        fixture.tick()
        owner=fixture.coordinator
        before=next(session for session in owner.sessions if session.instance_id=='alpha')
        other=next(session for session in owner.sessions if session.instance_id=='beta')
        self.assertGreater(before.journal.pending_count(),0)
        cursor=before.last_local_id
        repo=ConfigRepository(fixture.root)
        item=repo.get('alpha');repo.request_instance('alpha','stop',item['revision'])
        control=owner.instance_control
        control.checked=0;control.tick()
        self.assertNotIn(before,owner.sessions)
        self.assertIs(next(session for session in owner.sessions if session.instance_id=='beta'),other)
        owner.save_status()
        import json
        stopped_snapshot=json.loads(owner.status_path.read_text(encoding='utf-8'))['inactive_queues']['alpha']
        self.assertEqual(stopped_snapshot['cursor'],cursor)
        self.assertGreater(stopped_snapshot['queue_length'],0)
        from wxbridge.app.operations import configured_groups
        checkpoint_ids={identifier for identifier,_,_ in configured_groups(fixture.root,fixture.root,include_disabled=True)}
        self.assertIn('alpha',checkpoint_ids)
        item=repo.get('alpha');repo.request_instance('alpha','start',item['revision'])
        with patch('wxbridge.app.runtime.instance_lock',side_effect=lambda value:nullcontext()):
            control.checked=0;control.tick()
        after=next(session for session in owner.sessions if session.instance_id=='alpha')
        self.assertEqual(after.last_local_id,cursor)
        self.assertGreater(after.journal.pending_count(),0)
        self.assertIsNot(after,before)
