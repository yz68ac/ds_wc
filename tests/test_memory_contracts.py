"""Read-only memory boundaries preserve evidence and legacy public behavior."""
from copy import deepcopy
import ast
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_memory as memory_tests
import test_context_pipeline as pipeline
import test_proactive as proactive_tests
from synthetic import NOW, msg
from wxbridge.ai.memory import snapshot
from wxbridge.storage.memory_render import render_memory


class MemoryContractTests(unittest.TestCase):
    setUp = memory_tests.MemoryTests.setUp
    snap = memory_tests.MemoryTests.snap

    def seeded(self):
        self.memory.apply(memory_tests.suggestion(), self.snap(), NOW)
        return self.memory.view()

    def test_view_is_recursively_read_only_and_detached(self):
        view = self.seeded()
        with self.assertRaises(TypeError):
            view.members['wxid_a']['entries'][0]['summary'] = 'changed'
        with self.assertRaises(TypeError):
            view.identity['chat'] = 'other@chatroom'
        original = view.members['wxid_a']['entries'][0]['summary']
        self.memory.data['members']['wxid_a']['entries'][0]['summary'] = 'new repository value'
        self.assertEqual(view.members['wxid_a']['entries'][0]['summary'], original)

    def test_background_is_json_serializable_and_detached_from_view(self):
        view = self.seeded()
        result = view.background({'wxid_a': 'U1'}, NOW)
        json.dumps(result)
        result['U1']['entries'][0]['summary'] = 'edited by consumer'
        self.assertNotEqual(view.members['wxid_a']['entries'][0]['summary'], 'edited by consumer')
        self.assertNotIn('wxid_a', repr(view))

    def test_snapshot_consumes_one_consistent_revision_not_live_store(self):
        view = self.seeded()
        question = msg(2, NOW+1, '新的普通偏好', 'wxid_a')
        before = deepcopy(self.memory.data)
        value = snapshot(self.cfg, [question], [question], memory_tests.IDENTITY, view, NOW+1)
        json.dumps(value)
        self.assertEqual(value['revision'], view.revision)
        self.assertEqual(value['watermark'], view.watermark)
        self.assertEqual(value['messages'][0]['id'], 'm2')
        self.assertEqual(self.memory.data, before)

    def test_cross_identity_view_is_rejected(self):
        view = self.seeded()
        identity = {**memory_tests.IDENTITY, 'chat': 'other@chatroom'}
        question = msg(2, NOW+1, '普通提问', 'wxid_a')
        with self.assertRaisesRegex(ValueError, 'memory_identity_mismatch'):
            snapshot(self.cfg, [], [question], identity, view, NOW+1)

    def test_rendering_and_legacy_store_methods_agree(self):
        view = self.seeded()
        labels = {'wxid_a': 'U1'}
        self.assertEqual(render_memory(view, labels, 'wxid_a', NOW), self.memory.prompt(labels, 'wxid_a', NOW))
        self.assertEqual(view.reply_relationship('wxid_a', NOW), self.memory.reply_relationship('wxid_a', NOW))

    def test_storage_rules_do_not_import_model_layer(self):
        root = Path(__file__).resolve().parents[1]/'src/wxbridge/storage'
        for path in root.glob('*.py'):
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                names = ([node.module or ''] if isinstance(node, ast.ImportFrom) else
                         [item.name for item in node.names] if isinstance(node, ast.Import) else [])
                self.assertFalse(any(name.startswith('wxbridge.ai') for name in names), str(path))

    def test_foreground_render_and_relationship_share_one_view(self):
        self.seeded()
        case = pipeline.PipelineTests('runTest')
        case.setUp(); self.addCleanup(case.doCleanups)
        case.bot.memory = self.memory
        case.add(2, sender='wxid_a')
        with patch.object(self.memory, 'view', wraps=self.memory.view) as capture:
            case.bot.tick()
        self.assertEqual(capture.call_count, 1)

    def test_many_participation_candidates_share_one_view(self):
        self.seeded()
        case = proactive_tests.ProactivePipelineTests('runTest')
        case.setUp(); self.addCleanup(case.doCleanups)
        case.bot.memory = self.memory
        with patch.object(self.memory, 'view', wraps=self.memory.view) as capture:
            case.chat(count=10)
        self.assertTrue(any(job.kind == 'proactive' for job in case.jobs))
        self.assertEqual(capture.call_count, 1)
