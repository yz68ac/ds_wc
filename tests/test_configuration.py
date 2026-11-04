"""Typed configuration, immutable snapshots and document boundaries; no live data."""
from dataclasses import FrozenInstanceError, replace
import json
import os
from pathlib import Path
import pickle
import multiprocessing as mp
import tempfile
import unittest
from unittest.mock import patch

from wxbridge.config import (AppConfig, ConfigError, FrozenMap, InstanceConfig, UNSET,
    default_document, configuration_hash, load_account, load_config, prepare_runtime, resolve_instance)
from wxbridge.config.codec import json_value
from wxbridge.ai.roles import image_request
from tests.support.paths import ROOT


def _spawn_settings(pipe, settings):
    try:
        pipe.send((settings.instances['alpha'].target.context_max_chars,
                   isinstance(settings.instances,FrozenMap)))
    finally:
        pipe.close()


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'.test-tmp')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.path=self.root/'config.json'
        self.path.write_text(json.dumps(dict(provider_env=str(self.root/'empty.env'),
            queue=dict(file='runtime/queue.sqlite3'),state_file='runtime/state.json',memory=dict(directory='memory'))))
        inherited={key:value for key,value in os.environ.items()
                   if not key.startswith(('WX_','OPENAI_','ANTHROPIC_','DEEPSEEK_','GEMINI_'))}
        env=patch.dict(os.environ,inherited,clear=True);env.start();self.addCleanup(env.stop)

    def instance(self,name='alpha',**overrides):
        directory=self.root/'instance'/name;directory.mkdir(parents=True,exist_ok=True)
        document=dict(chat='synthetic_'+name+'@chatroom',enabled=False,system_prompt_file='system.md')
        document.update(overrides)
        (directory/'config.json').write_text(json.dumps(document))
        return directory/'config.json'

    def test_reference_matches_class_defaults_and_returns_detached_documents(self):
        expected=json.loads((ROOT/'config.defaults.json').read_text(encoding='utf-8'))
        self.assertEqual(default_document(),expected)
        self.assertFalse(expected['vision']['context_images_enabled'])
        changed=default_document();changed['participation']['address_names'].append('changed')
        self.assertEqual(default_document(),expected)

    def test_legacy_tool_rounds_are_migrated_when_loading_instances_without_rewriting(self):
        path=self.instance(search={'max_tool_rounds':3})
        before=path.read_bytes()
        value=load_config(self.path).instances['alpha']
        self.assertEqual(value.tools.max_rounds,3)
        self.assertNotIn('max_tool_rounds',json_value(value.search))
        self.assertEqual(value.metadata.origins['tools.max_rounds'],'instance.alpha:search.max_tool_rounds')
        self.assertNotIn('search.max_tool_rounds',value.metadata.origins)
        self.assertEqual(path.read_bytes(),before)

    def test_tool_and_search_timeouts_are_independent_when_loading_instances(self):
        path=self.instance(search={'enabled': True, 'total_timeout_seconds': 35},
                           tools={'total_timeout_seconds': 200})
        before=path.read_bytes()
        value=load_config(self.path).instances['alpha']
        self.assertEqual(value.search.total_timeout_seconds,35)
        self.assertEqual(value.tools.total_timeout_seconds,200)
        self.assertEqual(path.read_bytes(),before)
        self.instance(search={'enabled': False, 'total_timeout_seconds': 40})
        value=load_config(self.path).instances['alpha']
        self.assertEqual(value.search.total_timeout_seconds,40)
        self.assertEqual(value.tools.total_timeout_seconds,120)

    def test_tool_timeout_uses_default_and_rejects_invalid_values(self):
        path=self.instance(search={'total_timeout_seconds': 35})
        before=path.read_bytes()
        value=load_config(self.path).instances['alpha']
        self.assertEqual(value.search.total_timeout_seconds,35)
        self.assertEqual(value.tools.total_timeout_seconds,120)
        self.assertEqual(path.read_bytes(),before)
        for timeout in (0,301,True,'120'):
            with self.subTest(timeout=timeout):
                self.instance(tools={'total_timeout_seconds': timeout})
                with self.assertRaises(ConfigError):load_config(self.path)

    def test_explicit_tool_rounds_override_legacy_and_validate_bounds(self):
        self.instance(search={'max_tool_rounds':3},tools={'max_rounds':1})
        self.assertEqual(load_config(self.path).instances['alpha'].tools.max_rounds,1)
        for value in (0,4,True,'2'):
            with self.subTest(value=value):
                self.instance(search={'max_tool_rounds':3},tools={'max_rounds':value})
                with self.assertRaises(ConfigError):load_config(self.path)

    def test_legacy_tool_rounds_still_validate_bounds(self):
        for value in (0,4,True,'2'):
            with self.subTest(value=value):
                self.instance(search={'max_tool_rounds':value})
                with self.assertRaises(ConfigError):load_config(self.path)

    def test_load_does_not_read_default_reference(self):
        original=Path.read_text
        def checked(path,*args,**kwargs):
            self.assertNotEqual(path.name,'config.defaults.json')
            return original(path,*args,**kwargs)
        with patch.object(Path,'read_text',checked):
            self.assertIsInstance(load_config(self.path),AppConfig)

    def test_nested_settings_and_collections_are_immutable(self):
        self.instance();app=load_config(self.path);value=app.instances['alpha']
        for action in (lambda:setattr(value.participation,'enabled',True),
                       lambda:setattr(value.account.ui,'dry_run',False)):
            with self.assertRaises(FrozenInstanceError):action()
        with self.assertRaises(TypeError):app.instances['extra']=value
        self.assertIsInstance(value.account.helper_transport.allowed_chat_ids,tuple)

    def test_account_is_shared_readonly_and_instance_replacement_is_independent(self):
        self.instance();self.instance('beta');app=load_config(self.path)
        first,second=app.instances.values()
        self.assertIs(first.account,second.account)
        changed=replace(first,target=replace(first.target,context_max_chars=8000))
        self.assertEqual(second.target.context_max_chars,15000)
        self.assertNotEqual(configuration_hash(changed),configuration_hash(first))

    def test_runtime_overrides_keep_desired_hash_and_are_idempotent(self):
        self.instance();app=load_config(self.path);before=pickle.dumps(app)
        runtime=prepare_runtime(app,dry_run=True,keys_file=self.root/'temporary-keys.json')
        self.assertEqual(pickle.dumps(app),before)
        self.assertEqual(configuration_hash(runtime),configuration_hash(app))
        for name,value in runtime.instances.items():
            self.assertTrue(configuration_hash(value))
            self.assertEqual(configuration_hash(value),configuration_hash(app.instances[name]))
        self.assertTrue(runtime.instances['alpha'].paths.queue_file.endswith('.dry-run'))
        self.assertIs(prepare_runtime(runtime),runtime)
        self.assertTrue(runtime.account.ui.verify_chat_selected)

    def test_pickle_round_trip_preserves_frozen_types_and_identity_graph(self):
        self.instance();self.instance('beta');app=pickle.loads(pickle.dumps(load_config(self.path)))
        self.assertIsInstance(app,AppConfig)
        self.assertIs(app.account,app.instances['alpha'].account)
        self.assertIs(app.account,app.instances['beta'].account)

    def test_spawn_transfers_only_the_detached_settings_snapshot(self):
        self.instance();app=load_config(self.path)
        context=mp.get_context('spawn');parent,child=context.Pipe(duplex=False)
        process=context.Process(target=_spawn_settings,args=(child,app))
        try:
            process.start();child.close()
            self.assertTrue(parent.poll(10),'spawn settings response timed out')
            self.assertEqual(parent.recv(),(15000,True))
            process.join(10);self.assertEqual(process.exitcode,0)
        finally:
            if process.is_alive():process.terminate();process.join(5)
            parent.close();child.close()

    def test_provider_keys_are_excluded_even_after_enabled_model_validation(self):
        key='synthetic-private-provider-credential'
        (self.root/'empty.env').write_text('DEEPSEEK_API_KEY='+key)
        self.instance(enabled=True,models=dict(reply=dict(model='synthetic-model')),memory=dict(enabled=False))
        settings=load_config(self.path)
        self.assertNotIn(key,repr(settings))
        self.assertNotIn(key.encode(),pickle.dumps(settings))

    def test_absent_options_are_distinct_from_explicit_null(self):
        self.instance(models=dict(reply=dict(options=dict(max_tokens=None,reasoning_effort=None))))
        cfg=load_config(self.path).instances['alpha']
        self.assertIsNone(cfg.models.reply.options.max_tokens)
        self.assertIsNone(cfg.models.reply.options.reasoning_effort)
        self.assertIs(cfg.models.reply.options.temperature,UNSET)
        self.assertNotIn('temperature',json_value(cfg.models.reply.options))

    def test_image_options_preserve_vendor_specific_parameters(self):
        self.instance(models=dict(image=dict(model='synthetic',options=dict(aspect_ratio='16:9',image_size='1K'))))
        cfg=load_config(self.path).instances['alpha'];request=image_request(cfg,'synthetic drawing')
        self.assertEqual(request.options,dict(timeout_seconds=180,aspect_ratio='16:9',image_size='1K'))
        self.assertNotIn('quality',request.options)

    def test_metadata_is_separate_and_unknown_allowed_fields_are_frozen(self):
        self.instance(target=dict(unused=dict(items=[1,2])))
        cfg=load_config(self.path).instances['alpha']
        self.assertTrue(cfg.metadata.revision)
        self.assertNotIn('_config_origins',json_value(cfg.target))
        self.assertEqual(cfg.target.extensions['unused']['items'],(1,2))
        self.assertFalse(hasattr(cfg.target,'unused'))

    def test_account_loading_does_not_require_valid_instance_files(self):
        file=self.instance();file.write_text('invalid')
        self.assertTrue(load_account(self.path).metadata.path)
        with self.assertRaises(ConfigError):load_config(self.path)

    def test_bad_types_report_field_without_value(self):
        self.instance(target=dict(context_max_chars='PRIVATE_CONFIG_VALUE'))
        with self.assertRaises(ValueError) as caught:load_config(self.path)
        self.assertNotIn('PRIVATE_CONFIG_VALUE',str(caught.exception))
