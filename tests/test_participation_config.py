"""Unified participation config and explicit conversion; synthetic data only."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from wxbridge.config import DEFAULTS, migrate_instance_data, resolve_config
from wxbridge.ai.roles import validate_models


class ParticipationConfigTests(unittest.TestCase):
    def resolve(self, data):
        return resolve_config(data, '__synthetic_config__.json', environment=False,
                              validate_role_models=False)

    def test_defaults_have_one_decision_path_and_no_direct_trigger_configuration(self):
        cfg = self.resolve({})
        self.assertEqual(cfg['participation'], dict(enabled=False, debounce_seconds=2,
            max_wait_seconds=5, max_batch_messages=8, address_names=[], pat_enabled=True))
        self.assertNotIn('proactive', cfg)
        self.assertNotIn('trigger', cfg['target'])
        self.assertNotIn('debounce_seconds', cfg['queue'])

    def test_valid_partial_overrides_do_not_change_other_models_or_call_migration(self):
        data = dict(participation=dict(enabled=True, debounce_seconds=0, max_batch_messages=16,
                                       address_names=['助手']), target=dict(max_message_age_seconds=45))
        before = deepcopy(data)
        with patch('wxbridge.config.migrate_instance_data', side_effect=AssertionError('runtime conversion')):
            cfg = self.resolve(data)
        self.assertEqual(data, before)
        self.assertEqual(cfg['participation']['debounce_seconds'], 0)
        self.assertEqual(cfg['models'], DEFAULTS['models'])
        self.assertEqual(cfg['target']['max_message_age_seconds'], 45)

    def test_legacy_keys_are_rejected_instead_of_runtime_fallback(self):
        for data in (dict(proactive={}), dict(target=dict(trigger={})),
                     dict(queue=dict(debounce_seconds=0))):
            with self.subTest(data=data), self.assertRaisesRegex(ValueError, 'obsolete_model_configuration'):
                self.resolve(data)

    def test_invalid_decision_settings_and_expiry_are_rejected(self):
        for value in (dict(enabled=1), dict(pat_enabled=0), dict(debounce_seconds=True),
                      dict(debounce_seconds=-1), dict(debounce_seconds=float('nan')),
                      dict(max_wait_seconds=0), dict(max_wait_seconds=1),
                      dict(max_batch_messages=True), dict(max_batch_messages=0),
                      dict(max_batch_messages=33), dict(address_names='助手'),
                      dict(address_names=['a\nb']), dict(min_messages=3)):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.resolve(dict(participation=value))
        for value in (False, 0, -1, float('inf'), 3601):
            with self.subTest(age=value), self.assertRaises(ValueError):
                self.resolve(dict(target=dict(max_message_age_seconds=value)))

    def test_explicit_conversion_preserves_identity_model_parameters_and_other_business_values(self):
        old = dict(enabled=False, chat='123@chatroom', system_prompt_file='private.md',
            models=dict(reply=dict(model='synthetic-reply', options=dict(thinking='enabled', reasoning_effort='high'))),
            model_clients=dict(reply='anthropic', image='openai', background='deepseek'),
            memory=dict(enabled=False), profile=dict(enabled=True),
            target=dict(bot_name='小助手', max_message_age_seconds=75,
                        trigger=dict(mention_enabled=True, keyword_enabled=False, pat_enabled=False,
                                     mentions=['@助手', '额外称呼'])),
            proactive=dict(enabled=True, window_seconds=600,
                           opportunities=dict(address_names=['额外称呼']), min_messages=10),
            queue=dict(debounce_seconds=7, max_pending=64))
        before = deepcopy(old)
        new = migrate_instance_data(old)
        self.assertEqual(old, before)
        for key in ('enabled', 'chat', 'system_prompt_file', 'models', 'model_clients', 'memory', 'profile'):
            self.assertEqual(new[key], old[key])
        self.assertEqual(new['target'], dict(bot_name='小助手', max_message_age_seconds=75,
                                             context_window_seconds=600))
        self.assertEqual(new['queue'], dict(max_pending=64))
        self.assertEqual(new['participation'], dict(enabled=True, debounce_seconds=7,
            max_wait_seconds=7, max_batch_messages=8, address_names=['额外称呼', '助手'], pat_enabled=False))
        self.assertNotIn('proactive', new)
        self.assertEqual(migrate_instance_data(new), new)

    def test_conversion_keeps_new_overrides_and_explicit_zero_debounce(self):
        old = dict(proactive=dict(enabled=False), queue=dict(debounce_seconds=0),
                   participation=dict(enabled=True, max_batch_messages=3))
        new = migrate_instance_data(old)
        self.assertTrue(new['participation']['enabled'])
        self.assertEqual(new['participation']['max_batch_messages'], 3)
        self.assertEqual(new['participation']['debounce_seconds'], 0)

    def test_model_id_is_required_when_participation_is_enabled(self):
        cfg = deepcopy(DEFAULTS)
        cfg['models']['reply']['model'] = 'synthetic-reply'
        cfg['memory']['enabled'] = False
        config = SimpleNamespace(connections={name: object() for name in ('openai', 'anthropic', 'deepseek')})
        with patch('wxbridge.ai.roles.load_config', return_value=config):
            validate_models(cfg)
            cfg['participation']['enabled'] = True
            with self.assertRaisesRegex(ValueError, 'missing_model:participation'):
                validate_models(cfg)

    def test_jsonc_example_contains_only_supported_participation_fields(self):
        filename = Path(__file__).resolve().parents[1]/'examples/instance/config.example.jsonc'
        value = json.loads('\n'.join(line for line in filename.read_text(encoding='utf-8').splitlines()
                                     if not line.lstrip().startswith('//')))
        self.assertEqual(set(value['participation']), set(DEFAULTS['participation']))
        self.assertNotIn('proactive', value)
        self.assertNotIn('trigger', value['target'])


if __name__ == '__main__':
    unittest.main()
