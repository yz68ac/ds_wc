"""Resource ownership, independent transports and serialized image-job boundaries."""
from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, MagicMock, patch

from tests.support.configuration import replace_config
from tests.support.model_support import default_config
from tests.support.paths import ROOT
from wxbridge.config import load_account, ConfigError
from wxbridge.config.compatibility import normalize_transport_config
from wxbridge.contracts.image_jobs import QueuedImageJob
from wxbridge.native.settings import settings as helper_settings
from wxbridge.submission_api.protocol import settings as submission_settings
from wxbridge.app.sessions.conversation import ConversationSession


class ConstructionTests(unittest.TestCase):
    def test_every_construction_failure_releases_all_previously_acquired_resources(self):
        cfg=default_config()
        cfg=replace_config(cfg,'account.ui.dry_run',False)
        cfg=replace_config(cfg,'account.submission_api.enabled',True)
        cfg=replace_config(cfg,'profile.enabled',True)
        paths={
            'journal':'wxbridge.app.sessions.conversation.Journal',
            'memory':'wxbridge.app.sessions.conversation.MemoryController',
            'models':'wxbridge.app.execution.model_runtime.ConversationModels',
            'images':'wxbridge.app.tasks.image_generation.ImageGenerationController',
            'participation':'wxbridge.app.sessions.conversation.ParticipationController',
            'profile':'wxbridge.app.tasks.group_profile.ProfileController',
            'server':'wxbridge.submission_api.server.SubmissionServer',
        }
        order=list(paths)
        for failure in ('recover','memory','models','participation','profile','server'):
            with self.subTest(failure=failure), ExitStack() as patches:
                resources={name:MagicMock() for name in order}
                factories={name:patches.enter_context(patch(path,return_value=resources[name])) for name,path in paths.items()}
                patches.enter_context(patch('wxbridge.app.sessions.conversation.runtime_identity',return_value={}))
                if failure=='recover':resources['journal'].recover.side_effect=RuntimeError('construction_failed')
                else:factories[failure].side_effect=RuntimeError('construction_failed')
                store=Mock(self_wxid='wxid_bot');store.resolve_chat.return_value=('synthetic@chatroom','synthetic');store.last_local_id.return_value=0
                with self.assertRaisesRegex(RuntimeError,'construction_failed'):ConversationSession(cfg,store=store)
                acquired=order[:1] if failure=='recover' else order[:order.index(failure)]
                for name in acquired:
                    cleanup=resources[name].cancel if name=='participation' else resources[name].close
                    cleanup.assert_called_once()
                for name in set(order)-set(acquired):
                    cleanup=resources[name].cancel if name=='participation' else resources[name].close
                    cleanup.assert_not_called()


class TransportConfigurationTests(unittest.TestCase):
    def test_helper_can_send_without_opening_the_submission_api(self):
        cfg=replace_config(default_config(),'account.helper_transport.enabled',True)
        self.assertTrue(helper_settings(cfg).enabled)
        self.assertFalse(submission_settings(cfg).enabled)
        cfg=replace_config(cfg,'account.ui.dry_run',False)
        from wxbridge.native.sender import submit
        with patch('wxbridge.native.sender.NativeSender') as sender:
            sender.return_value.submit_text.return_value.metadata={'submitted':True}
            self.assertEqual(submit(cfg,'filehelper','synthetic','request')['submitted'],True)
            sender.assert_called_once_with(cfg.account.helper_transport.socket,max_text_bytes=4096)

    def test_submission_enabled_is_not_an_implicit_helper_enable(self):
        cfg=replace_config(default_config(),'account.submission_api.enabled',True)
        self.assertTrue(submission_settings(cfg).enabled)
        self.assertFalse(helper_settings(cfg).enabled)

    def test_legacy_document_keeps_both_old_enable_behaviors_without_rewriting_files(self):
        value={'native_api':{'enabled':True,'socket':'/run/synthetic/api.sock',
                 'helper_socket':'/run/synthetic/helper.sock','allowed_chat_ids':['filehelper'],'max_pending':4}}
        expected=deepcopy(value)
        converted=normalize_transport_config(value)
        self.assertEqual(value,expected)
        self.assertTrue(converted['helper_transport']['enabled'])
        self.assertTrue(converted['submission_api']['enabled'])
        with tempfile.TemporaryDirectory(dir=ROOT/'.test-tmp') as directory:
            path=Path(directory)/'config.json';raw=json.dumps(value);path.write_text(raw,encoding='utf-8')
            account=load_account(path)
            self.assertEqual(account.helper_transport.socket,'/run/synthetic/helper.sock')
            self.assertEqual(account.submission_api.max_pending,4)
            self.assertEqual(path.read_text(encoding='utf-8'),raw)

    def test_mixed_legacy_and_new_sections_cannot_silently_override_each_other(self):
        with self.assertRaisesRegex(ConfigError,'mixed_native_configuration'):
            normalize_transport_config({'native_api':{'enabled':True},'helper_transport':{'enabled':False}})


class ImageJobContractTests(unittest.TestCase):
    def test_round_trip_detaches_options_and_source_and_rejects_inline_pixels(self):
        source=dict(file='a'*32+'.png',mime='image/png',bytes=20,sha256='b'*64,md5='c'*32,width=2,height=1)
        job=QueuedImageJob('synthetic','edit',{'quality':'low'},source)
        document=job.to_document();restored=QueuedImageJob.from_document(document)
        document['options']['quality']='high';document['source_image']['bytes']=100
        self.assertEqual(restored.options['quality'],'low')
        self.assertEqual(restored.source_image['bytes'],20)
        self.assertEqual(job.source_image['bytes'],20)
        invalid=job.to_document();invalid['source_image']['data']='pixels'
        with self.assertRaisesRegex(ValueError,'invalid_image_artifact_reference'):
            QueuedImageJob.from_document(invalid)

    def test_unexpected_job_fields_fail_before_model_invocation(self):
        for document in ({'model':'x','prompt':'p','options':{},'images':[]},
                         {'model':'x','prompt':'','options':{}},
                         {'model':'x','prompt':'p','options':None}):
            with self.subTest(document=document),self.assertRaisesRegex(ValueError,'invalid_queued_image_job'):
                QueuedImageJob.from_document(document)
