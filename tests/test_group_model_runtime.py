"""Thread-lane contracts using local functions and synthetic provider clients."""
from threading import Event, Thread
from copy import deepcopy
from concurrent.futures import CancelledError
from unittest import TestCase
from unittest.mock import Mock, patch

from provider import ImageRequest, ModelResult
from tests.support.model_support import default_config
from wxbridge.app.model_runtime import GroupModels, ModelLane


class GroupModelRuntimeTests(TestCase):
    def test_cancelling_queued_job_never_executes_it(self):
        lane = ModelLane('background', 'deepseek', None)
        started, release, called = Event(), Event(), Mock()
        try:
            first = lane.submit(lambda job: started.set() or release.wait(3) or {}, 10)
            self.assertTrue(started.wait(2))
            queued = lane.submit(called, 10)
            queued.close()
            release.set()
            first.future.result(timeout=2)
            with self.assertRaises(CancelledError): queued.future.result(timeout=2)
            called.assert_not_called()
        finally:
            release.set()
            lane.close()

    def test_running_cancelled_result_is_discarded(self):
        lane = ModelLane('reply', 'deepseek', None)
        started, release = Event(), Event()
        def run(job):
            started.set()
            release.wait(3)
            return dict(ok=True, reply='late result')
        try:
            job = lane.submit(run, 10)
            self.assertTrue(started.wait(2))
            job.close()
            release.set()
            self.assertFalse(job.future.result(timeout=2)['ok'])
        finally:
            release.set()
            lane.close()

    def test_close_waits_for_running_call_cancels_queue_then_closes_client(self):
        lane = ModelLane('reply', 'deepseek', None)
        started, release, closed = Event(), Event(), Event()
        client, queued = Mock(), Mock()
        lane.client = client
        def run(job):
            started.set()
            release.wait(3)
            return {}
        closer = None
        try:
            job = lane.submit(run, 10)
            self.assertTrue(started.wait(2))
            next_job = lane.submit(queued, 10)
            closer = Thread(target=lambda: (lane.close(), closed.set()))
            closer.start()
            self.assertFalse(closed.wait(0.05))
            client.close.assert_not_called()
            release.set()
            self.assertTrue(closed.wait(2))
            client.close.assert_called_once()
            self.assertTrue(next_job.future.cancelled())
            queued.assert_not_called()
            with self.assertRaisesRegex(Exception, 'client_closed'):
                lane.submit(queued, 5)
        finally:
            release.set()
            if closer: closer.join(2)
            else: lane.close()

    def test_reply_and_background_start_in_parallel_with_detached_request_options(self):
        cfg = default_config()
        cfg['models']['reply']['options'].update(thinking='enabled', reasoning_effort='high')
        cfg['models']['participation']['options'].update(thinking='disabled')
        saved = deepcopy(cfg)
        started, release, requests = [Event(), Event()], Event(), []
        def create(*args, **kwargs):
            client = Mock()
            def generate(request, **options):
                requests.append(request)
                index = 0 if request.model == 'reply-model' else 1
                started[index].set()
                release.wait(3)
                return ModelResult({'content': 'ok'}, 'stop', {})
            client.generate.side_effect = generate
            return client
        cfg['models']['reply']['model'] = 'reply-model'
        cfg['models']['participation']['model'] = 'judge-model'
        saved = deepcopy(cfg)
        with patch('wxbridge.app.model_runtime.create_client', side_effect=create):
            runtime = GroupModels(cfg)
            try:
                jobs = [runtime.submit('request', (cfg, role, []), timeout=5)
                        for role in ('reply', 'participation')]
                self.assertTrue(all(event.wait(2) for event in started))
                cfg['models']['reply']['options']['reasoning_effort'] = 'low'
                release.set()
                self.assertTrue(all(job.future.result(timeout=2)['ok'] for job in jobs))
                by_model = {r.model: r.options for r in requests}
                self.assertEqual(by_model['reply-model']['reasoning_effort'], 'high')
                self.assertEqual(by_model['judge-model']['thinking'], 'disabled')
                self.assertEqual(runtime.cfg, saved)
            finally:
                release.set()
                runtime.close()
    def test_fifo_and_running_cancellation_do_not_release_the_lane(self):
        lane = ModelLane('reply', 'deepseek', None)
        started, release, second = Event(), Event(), Event()
        def first(job):
            started.set()
            release.wait(5)
            return {'ok': True}
        try:
            job = lane.submit(first, 10)
            self.assertTrue(started.wait(2))
            next_job = lane.submit(lambda job: second.set() or {'ok': True}, 10)
            job.close()
            self.assertFalse(second.is_set())
            release.set()
            next_job.future.result(timeout=2)
            self.assertTrue(second.is_set())
        finally:
            release.set()
            lane.close()

    def test_groups_reuse_their_own_clients_and_roles_keep_options(self):
        cfg = default_config()
        cfg['models']['memory']['model'] = 'memory-model'
        cfg['models']['avatar']['model'] = 'avatar-model'
        cfg['models']['group_profile']['model'] = 'profile-model'
        clients = []
        def create(*args, **kwargs):
            client = Mock()
            client.generate.return_value = ModelResult({'content': 'ok'}, 'stop', {})
            clients.append(client)
            return client
        with patch('wxbridge.app.model_runtime.create_client', side_effect=create):
            first, second = GroupModels(cfg), GroupModels(cfg)
            try:
                for role in ('memory', 'avatar', 'group_profile'):
                    first.submit('request', (cfg, role, []), timeout=5).future.result(timeout=2)
                second.submit('request', (cfg, 'memory', []), timeout=5).future.result(timeout=2)
                self.assertEqual(len(clients), 2)
                calls = clients[0].generate.call_args_list
                self.assertEqual([c.args[0].model for c in calls], ['memory-model', 'avatar-model', 'profile-model'])
                self.assertIsNot(first.lanes['background'].client, second.lanes['background'].client)
            finally:
                first.close()
                second.close()
            for client in clients:
                client.close.assert_called_once()

    def test_expired_image_request_never_creates_a_client(self):
        runtime = GroupModels(default_config())
        try:
            with patch('wxbridge.app.model_runtime.create_client') as create:
                result = runtime.submit_images(ImageRequest('image-model', 'synthetic'), deadline=0)
                with self.assertRaises(Exception):
                    result.result(timeout=2)
                create.assert_not_called()
        finally:
            runtime.close()

    def test_batch_decision_uses_background_client_and_full_reply_uses_reply_client(self):
        cfg = default_config()
        cfg['search']['enabled'] = False
        cfg['models']['participation']['model'] = 'judge-model'
        cfg['models']['reply']['model'] = 'reply-model'
        cfg['models']['memory']['model'] = 'memory-model'
        cfg['models']['reply']['options'].update(thinking='enabled', reasoning_effort='high')
        cfg['models']['participation']['options'].update(thinking='disabled')
        saved = deepcopy(cfg)
        clients, requests = [], []
        text = '完整回复不受旧的三十字短答限制。'*6
        def create(*args, **kwargs):
            client = Mock()
            def generate(request, **options):
                requests.append(request)
                content = '{"reply_to":[2,1]}' if request.model == 'judge-model' else text
                return ModelResult({'content': content}, 'stop', {})
            client.generate.side_effect = generate
            clients.append(client)
            return client
        prompt = [dict(role='system', content='合成规则'), dict(role='user', content='合成上下文')]
        with patch('wxbridge.app.model_runtime.create_client', side_effect=create):
            runtime = GroupModels(cfg)
            try:
                decision = runtime.submit('participation', (cfg, prompt, dict(source_ids=[1,2])),
                                          timeout=5).future.result(timeout=2)
                self.assertTrue(decision['ok'])
                self.assertEqual(decision['decision'], dict(reply_to=[1,2]))
                self.assertIs(runtime.lanes['background'].client, clients[0])
                answer = runtime.submit('generate', (cfg, prompt, None), timeout=5).future.result(timeout=2)
                self.assertTrue(answer['ok'])
                self.assertEqual(answer['reply'], text)
                self.assertIs(runtime.lanes['reply'].client, clients[1])
                runtime.submit('request', (cfg, 'memory', []), timeout=5).future.result(timeout=2)
                self.assertEqual(len(clients), 2)
                self.assertEqual(clients[0].generate.call_count, 2)
                self.assertEqual(clients[1].generate.call_count, 1)
                self.assertEqual([request.model for request in requests], ['judge-model','reply-model','memory-model'])
                self.assertEqual(requests[0].options['thinking'], 'disabled')
                self.assertEqual(requests[1].options['reasoning_effort'], 'high')
                self.assertEqual(cfg, saved)
            finally:
                runtime.close()
        for client in clients:
            client.close.assert_called_once()
