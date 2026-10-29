"""Actual wxid regressions through memory scheduling and immutable AI snapshots."""
from copy import deepcopy
import json
import unittest
from unittest.mock import Mock, patch

import test_context_pipeline as pipeline
import test_memory as memory_tests
from synthetic import NOW
from wxbridge.ai.memory import extract


class MemoryIdentityPipelineTests(unittest.TestCase):
    setUp = pipeline.PipelineTests.setUp
    make_bot = pipeline.PipelineTests.make_bot
    add = pipeline.PipelineTests.add
    generated = pipeline.PipelineTests.generated
    sends = pipeline.PipelineTests.sends

    def test_avatar_enabled_memory_task_accepts_actual_wxid_and_deterministic_order(self):
        memory_tests.MemoryPipelineTests.enable(self)
        self.store.avatar.return_value = dict(available=False, error='avatar_unavailable')
        self.add(1, sender='wxid_a', mention=False)
        self.add(2, sender='wxid_z')
        self.bot.tick()
        self.generated()
        self.add(3, sender='wxid_self', text='合成回复', mention=False)
        self.bot.tick()
        self.assertIsNotNone(self.bot.memory_job, self.bot.memory_error)
        _, value, job = self.bot.memory_job
        self.assertEqual(job.kind, 'memory')
        self.assertEqual(set(value['members']), {'wxid_a', 'wxid_z'})
        self.assertEqual([c.args[0] for c in self.store.avatar.call_args_list], ['wxid_a', 'wxid_z'])
        self.assertEqual(value['members']['wxid_z']['label'], 'wxid_z')


class MemoryRequestSnapshotTests(unittest.TestCase):
    setUp = memory_tests.MemoryTests.setUp
    snap = memory_tests.MemoryTests.snap

    def test_building_memory_request_does_not_modify_its_existing_records(self):
        self.memory.apply(memory_tests.suggestion(), self.snap(), NOW)
        value = self.snap(2)
        saved = deepcopy(value)
        client = Mock()
        client.complete.return_value = dict(content='{"updates":[]}')
        with patch('wxbridge.ai.client.client_for', return_value=client):
            extract(self.cfg, value)
        public = json.loads(client.complete.call_args.args[0][1]['content'])
        self.assertEqual(public['members'], {'wxid_a': dict(alias='同名')})
        self.assertEqual(value, saved)
