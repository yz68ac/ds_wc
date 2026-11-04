"""Offline upgrade/rollback faults; synthetic files, no host service mutations."""
from copy import deepcopy
import hashlib
import importlib.util
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from tests.support.paths import ROOT
from tests.support.artifacts import adapter_artifact

spec = importlib.util.spec_from_file_location('upgrade_installation', ROOT/'scripts/upgrade-installation.py')
upgrade = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upgrade)


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=ROOT/'.test-tmp')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_file_rollback_restores_replaced_deleted_and_new_files(self):
        first, retired, added = [self.root/name for name in ('first.py', 'retired.py', 'new.py')]
        first.write_bytes(b'original'); retired.write_bytes(b'retired')
        private = self.root/'.env'; private.write_bytes(b'synthetic-private')
        changes = {first:(b'replacement',0o644), retired:(None,0), added:(b'new',0o644)}
        transaction = upgrade.FileTransaction(self.root/'backup')
        transaction.snapshot(changes); transaction.write(changes)
        self.assertFalse(retired.exists())
        upgrade.FileTransaction(self.root/'backup').restore()
        self.assertEqual(first.read_bytes(),b'original')
        self.assertEqual(retired.read_bytes(),b'retired')
        self.assertFalse(added.exists())
        self.assertEqual(private.read_bytes(),b'synthetic-private')

    def test_partial_write_can_be_rolled_back_after_atomic_replace_failure(self):
        first, second = self.root/'one', self.root/'two'
        first.write_bytes(b'one'); second.write_bytes(b'two')
        transaction = upgrade.FileTransaction(self.root/'backup')
        changes = {first:(b'updated',0o644),second:(b'updated',0o644)}
        transaction.snapshot(changes)
        replace = upgrade.os.replace
        def fail_second(source, target):
            if target == second:
                raise OSError('synthetic failure')
            return replace(source,target)
        with patch.object(upgrade.os,'replace',side_effect=fail_second), self.assertRaises(OSError):
            transaction.write(changes)
        transaction.restore()
        self.assertEqual((first.read_bytes(),second.read_bytes()),(b'one',b'two'))
        self.assertFalse(list(self.root.glob('.upgrade-*')))

    def test_context_conversion_preserves_models_identity_and_custom_settings(self):
        data=dict(enabled=False,chat='synthetic@chatroom',system_prompt_file='private.md',
                  target=dict(context_window_seconds=600,context_recent_messages=12),
                  memory=dict(enabled=False),model_clients=dict(reply='synthetic'),
                  models=dict(reply=dict(model='synthetic-model')),custom=dict(kept=True))
        original=deepcopy(data)
        migrated=upgrade.context_upgrade(data,15000)
        self.assertEqual(data,original)
        self.assertEqual(migrated['memory'],dict(enabled=False,context_window_seconds=600))
        self.assertEqual(migrated['target'],dict(context_recent_messages=12,context_max_age_seconds=1800,
                         context_bot_messages=3,context_budget_strategy='recent',context_max_chars=15000))
        for key in ('enabled','chat','system_prompt_file','model_clients','models','custom'):
            self.assertEqual(migrated[key],original[key])
        self.assertEqual(upgrade.context_upgrade(migrated,15000),migrated)

    def test_conflicting_memory_window_requires_review(self):
        with self.assertRaisesRegex(ValueError,'conflicting_memory_context_window'):
            upgrade.context_upgrade(dict(target=dict(context_window_seconds=600),
                                          memory=dict(context_window_seconds=300)),15000)

    def test_unit_conversion_preserves_lan_binding_and_security_options(self):
        old='[Service]\nUser=root\nExecStart=/opt/wxbridge/.venv/bin/python /opt/wxbridge/scripts/service.py run --web-host 192.168.1.212\nSendSIGKILL=no\n'
        new=upgrade.rewrite_unit(old).decode()
        self.assertEqual(new,old.replace('[Service]\n','[Service]\nExecStartPre=/usr/bin/chmod 0750 /run/wxbridge-control\n')
                         .replace('/opt/wxbridge/scripts/service.py run --web-host 192.168.1.212','-m wxbridge --root /opt/wxbridge'))
        self.assertEqual(upgrade.rewrite_unit(new),new.encode())
        for text in (old+'ExecStart=/other\n',old.replace('/opt/wxbridge/.venv/bin/python','/unreviewed/python')):
            with self.assertRaises(ValueError):upgrade.rewrite_unit(text)

    def test_web_settings_migration_is_explicit_and_does_not_overwrite_existing(self):
        unit='[Service]\nExecStart=/opt/wxbridge/.venv/bin/python -m wxbridge_service --root /opt/wxbridge --web-host 192.168.1.212 --web-port 8740\n'
        expected=dict(host='192.168.1.212',port=8740)
        self.assertEqual(upgrade.migrate_web_settings(unit),expected)
        self.assertEqual(upgrade.migrate_web_settings(unit,expected),expected)
        with self.assertRaisesRegex(ValueError,'conflicting_web_settings'):
            upgrade.migrate_web_settings(unit,dict(host='127.0.0.1',port=8730))
        new=upgrade.rewrite_unit(unit).decode()
        self.assertNotIn('--web-host',new);self.assertNotIn('--web-port',new)
        self.assertEqual(upgrade.migrate_web_settings(new,expected),expected)
        for args in ('--web-host 0.0.0.0','--web-port 0','--web-port 8730 --web-port 8740','--unknown value'):
            with self.subTest(args=args),self.assertRaises(ValueError):
                upgrade.migrate_web_settings(new.strip()+' '+args+'\n')

    def test_service_state_migration_keeps_web_paused(self):
        original=dict(schema=2,bot_enabled=True,web_enabled=False)
        self.assertEqual(upgrade.migrate_service_state(original),original)
        self.assertEqual(upgrade.migrate_service_state(dict(schema=1,bot_enabled=False)),
                         dict(schema=2,bot_enabled=False,web_enabled=True))
        with self.assertRaises(ValueError):upgrade.migrate_service_state(dict(schema=1,bot_enabled='false'))

    def stage(self):
        app,stage=self.root/'app',self.root/'stage';app.mkdir();stage.mkdir()
        (stage/'scripts').mkdir()
        shutil.copyfile(ROOT/'scripts/check-repository.py',stage/'scripts/check-repository.py')
        name='src/wxbridge/__main__.py';source=stage/name;source.parent.mkdir(parents=True);source.write_bytes(b'new')
        manifest=dict(format=1,commit='a'*40,files={name:dict(sha256=upgrade.digest(source),mode='100644')})
        for relative in upgrade.RECOVERY_MODULES:
            if relative.startswith('wechat_linux/'):
                continue
            target=stage/'src'/relative;target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(ROOT/'src'/relative,target)
            manifest['files']['src/'+relative]=dict(sha256=upgrade.digest(target),mode='100644')
        adapter_artifact(stage)
        for artifact in (stage/'dependencies').iterdir():
            manifest['files'][artifact.relative_to(stage).as_posix()]=dict(sha256=upgrade.digest(artifact),mode='100644')
        (stage/'release-manifest.json').write_text(json.dumps(manifest))
        return app,stage,name

    def test_recovery_projection_imports_without_application_or_desktop_dependencies(self):
        app,stage,_=self.stage()
        with patch.object(upgrade,'APP',app),patch.object(upgrade,'SCANNER',self.root/'scanner'):
            upgrade.check_recovery_projection(stage)
        self.assertFalse((self.root/'scanner').exists())

    def test_unknown_live_changes_are_rejected_without_writing(self):
        app,stage,name=self.stage();target=app/name;target.parent.mkdir(parents=True);target.write_bytes(b'local edit')
        with patch.object(upgrade,'APP',app),self.assertRaisesRegex(ValueError,'reviewed_source_changed'):
            upgrade.source_updates(stage,dict(files={name:hashlib.sha256(b'old').hexdigest()}))
        self.assertEqual(target.read_bytes(),b'local edit')

    def test_reviewed_retirement_cannot_delete_private_configuration(self):
        app,stage,name=self.stage();private=app/'instance/test/config.json';private.parent.mkdir(parents=True)
        private.write_bytes(b'synthetic configuration')
        baseline=dict(files={name:None,'instance/test/config.json':upgrade.digest(private)})
        with patch.object(upgrade,'APP',app),self.assertRaisesRegex(ValueError,'private_path_in_upgrade_baseline'):
            upgrade.source_updates(stage,baseline)
        self.assertTrue(private.exists())

    def test_missing_or_tampered_wheel_is_rejected(self):
        stage,wheels=self.root/'stage',self.root/'wheels';stage.mkdir();wheels.mkdir()
        adapter=adapter_artifact(stage)
        shutil.copyfile(adapter,wheels/adapter.name)
        for project,prefix in [('wxbridge','wxbridge'),('model_provider','provider')]:
            source=stage/'src'/prefix/'__init__.py';source.parent.mkdir(parents=True);source.write_bytes(b'synthetic')
            with zipfile.ZipFile(wheels/(project+'-1-py3-none-any.whl'),'w') as archive:
                archive.writestr(prefix+'/__init__.py',b'synthetic')
                if project == 'wxbridge':
                    for name in ('web/__init__.py', 'web/backend/__init__.py'):
                        module=stage/'src'/name;module.parent.mkdir(parents=True,exist_ok=True)
                        module.write_bytes(b'synthetic')
                        archive.writestr(name,b'synthetic')
        self.assertEqual(len(upgrade.verify_wheels(stage,wheels)),3)
        target=wheels/'model_provider-1-py3-none-any.whl'
        with zipfile.ZipFile(target,'w') as archive:archive.writestr('provider/__init__.py',b'tampered')
        with self.assertRaisesRegex(ValueError,'wheel_source_mismatch'):upgrade.verify_wheels(stage,wheels)
        target.unlink()
        with self.assertRaisesRegex(ValueError,'missing_upgrade_wheel'):upgrade.verify_wheels(stage,wheels)

    def test_resume_preserves_a_deliberately_paused_bot(self):
        backup=self.root/'backups/release';backup.mkdir(parents=True)
        record=dict(phase='awaiting_acceptance',private_hashes={},configurations={},
                    original_state=dict(schema=1,bot_enabled=False),commit='a'*40)
        (backup/'upgrade.json').write_text(json.dumps(record))
        state=dict(components={key:dict(state='active') for key in ('wechat','sender')})
        with patch.object(upgrade,'BACKUPS',backup.parent),patch.object(upgrade,'control',return_value=state) as control,patch.object(upgrade,'run') as run:
            self.assertEqual(upgrade.finish(backup)['phase'],'complete')
        control.assert_called_once_with('status');run.assert_not_called()

    def test_acceptance_requires_the_new_supervisor_socket_and_a_paused_bot(self):
        with patch.object(upgrade,'control',side_effect=[FileNotFoundError(),
                dict(bot_enabled=True,components={'bot':{}}),dict(bot_enabled=False,components={'bot':{}})]), \
                patch.object(upgrade.time,'sleep'):
            upgrade.wait_supervisor()
        with patch.object(upgrade,'control',side_effect=FileNotFoundError()), \
                patch.object(upgrade.time,'monotonic',side_effect=[0,0,21]),patch.object(upgrade.time,'sleep'):
            with self.assertRaisesRegex(ValueError,'new_supervisor_not_ready'):upgrade.wait_supervisor()

    def test_wheel_missing_a_new_module_or_containing_another_package_is_rejected(self):
        stage,wheels=self.root/'stage',self.root/'wheels';stage.mkdir();wheels.mkdir()
        source=stage/'src/wxbridge';source.mkdir(parents=True)
        (source/'__init__.py').write_bytes(b'synthetic')
        (source/'control.py').write_bytes(b'new control module')
        wheel=wheels/'wxbridge-1-py3-none-any.whl'
        for entries in ({'wxbridge/__init__.py':b'synthetic'},
                        {'wxbridge/__init__.py':b'synthetic','wxbridge/control.py':b'new control module',
                         'wechat_linux/__init__.py':b'old SDK'}):
            with zipfile.ZipFile(wheel,'w') as archive:
                for name,data in entries.items():archive.writestr(name,data)
            with self.assertRaisesRegex(ValueError,'wheel_inventory_mismatch'):
                upgrade.verify_wheels(stage,wheels)

    def test_runtime_health_checks_transitive_requirements_without_unrelated_system_packages(self):
        from wxbridge_cli.install import dependency_health
        requires, version = importlib.metadata.requires, importlib.metadata.version
        visited = set()
        def requirements(name):
            visited.add(name)
            if name == 'synthetic-runtime':
                return []
            return [*(requires(name) or []), *(['synthetic-runtime>=2'] if name=='model-provider' else [])]
        def installed(name):
            return '1' if name=='synthetic-runtime' else version(name)
        with patch.object(importlib.metadata,'requires',side_effect=requirements), \
                patch.object(importlib.metadata,'version',side_effect=installed):
            failures = dependency_health()
        self.assertIn('synthetic-runtime: incompatible',failures)
        self.assertNotIn('apt-listchanges',visited)

    @unittest.skipIf(os.name=='nt','Linux venv symlink switch and rollback')
    def test_failed_configuration_validation_restores_environment_code_and_enable_state(self):
        self._assert_failed_upgrade_rolls_back()

    @unittest.skipIf(os.name=='nt','Linux venv symlink switch and rollback')
    def test_failed_supervisor_start_restores_environment_and_resets_start_limit(self):
        self._assert_failed_upgrade_rolls_back(supervisor_failure=True)

    def _assert_failed_upgrade_rolls_back(self, *, supervisor_failure=False):
        app,stage,name=self.stage();target=app/name;target.parent.mkdir(parents=True);target.write_bytes(b'old')
        current=app/'.venv';(current/'bin').mkdir(parents=True);(current/'bin/python').write_bytes(b'old python')
        environment=self.root/'environment';(environment/'bin').mkdir(parents=True)
        unit=self.root/'unit';unit.write_text('[Service]\nExecStart=/opt/wxbridge/.venv/bin/python /opt/wxbridge/scripts/service.py run\n')
        launcher=self.root/'launcher';launcher.write_bytes(b'old launcher')
        state=self.root/'state.json';state.write_text(json.dumps(dict(schema=1,bot_enabled=True)))
        backup=self.root/'backups/release';backup.mkdir(parents=True)
        record=dict(phase='prepared',stage=str(stage),environment=str(environment),
                    manifest=upgrade.digest(stage/'release-manifest.json'),wheels={},configurations={},
                    baseline=dict(files={name:upgrade.digest(target)}),
                    scanner_hashes={str(self.root/'scanner'/relative):None for relative in upgrade.RECOVERY_MODULES})
        (backup/'upgrade.json').write_text(json.dumps(record))
        def run(*args,**kwargs):
            if '-I' in args and not supervisor_failure:raise subprocess.CalledProcessError(1,['synthetic validation'])
        patches=dict(APP=app,UNIT=unit,LAUNCHER=launcher,STATE=state,WEB_CONFIG=self.root/'web.json',BACKUPS=backup.parent,SCANNER=self.root/'scanner')
        with patch.multiple(upgrade,**patches),patch.object(upgrade,'check_installed_release'), \
                patch.object(upgrade,'run',side_effect=run) as calls, \
                patch.object(upgrade,'wait_supervisor',side_effect=ValueError('new_supervisor_not_ready')), \
                patch.object(upgrade,'control',return_value=dict(components=dict(bot=dict(state='inactive')))):
            with self.assertRaises(ValueError if supervisor_failure else subprocess.CalledProcessError):upgrade.apply(backup)
        self.assertIn(('systemctl','reset-failed','wxbridge.service'),[call.args for call in calls.call_args_list])
        self.assertEqual(target.read_bytes(),b'old')
        self.assertFalse(current.is_symlink());self.assertEqual((current/'bin/python').read_bytes(),b'old python')
        self.assertEqual(launcher.read_bytes(),b'old launcher')
        self.assertTrue(json.loads(state.read_text())['bot_enabled'])
        self.assertEqual(json.loads((backup/'upgrade.json').read_text())['phase'],'rolled_back')
