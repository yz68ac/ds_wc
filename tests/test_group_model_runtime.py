"""Thread-lane contracts using local functions and synthetic provider clients."""
from threading import Event
from unittest import TestCase
from unittest.mock import Mock, patch

from provider import ImageRequest, ModelResult
from model_support import default_config
from wxbridge.app.model_runtime import GroupModels, ModelLane


class GroupModelRuntimeTests(TestCase):
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
        clients = []
        def create(*args, **kwargs):
            client = Mock()
            client.generate.return_value = ModelResult({'content': 'ok'}, 'stop', {})
            clients.append(client)
            return client
        with patch('wxbridge.app.model_runtime.create_client', side_effect=create):
            first, second = GroupModels(cfg), GroupModels(cfg)
            try:
                for role in ('memory', 'avatar'):
                    first.submit('request', (cfg, role, []), timeout=5).future.result(timeout=2)
                second.submit('request', (cfg, 'memory', []), timeout=5).future.result(timeout=2)
                self.assertEqual(len(clients), 2)
                calls = clients[0].generate.call_args_list
                self.assertEqual([c.args[0].model for c in calls], ['memory-model', 'avatar-model'])
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
