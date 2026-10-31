"""Discovery cache and instance requests are metadata only, with synthetic credentials."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from wxbridge_web.demo import seed
from wxbridge_web.models import ModelCatalog
from wxbridge_web.repository import ConfigRepository,ManagementError


class ModelCatalogTests(unittest.TestCase):
    def setUp(self):
        parent=Path(__file__).resolve().parents[1]/'.test-tmp';parent.mkdir(exist_ok=True)
        temporary=tempfile.TemporaryDirectory(dir=parent);self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);seed(self.root)
        self.repository=ConfigRepository(self.root)
        self.catalog=ModelCatalog(self.repository,demo=True);self.addCleanup(self.catalog.close)

    def test_cache_read_does_not_create_client_and_never_returns_credentials(self):
        with patch('wxbridge_web.models.create_client',side_effect=AssertionError('no live client')):
            result=self.catalog.read()
        provider=next(row for row in result['providers'] if row['id']=='anthropic')
        self.assertEqual(provider['models'][0]['id'],'claude-haiku-5-5')
        self.assertNotIn('synthetic-demo-not-a-real-key',str(result))
        self.assertNotIn('source_fingerprint',str(result))

    def test_changed_connection_credentials_make_old_catalog_stale(self):
        path=self.root/'provider.env'
        path.write_text(path.read_text().replace('ANTHROPIC_API_KEY=synthetic-demo-not-a-real-key','ANTHROPIC_API_KEY=synthetic-replaced'),encoding='utf-8')
        provider=next(row for row in self.catalog.read()['providers'] if row['id']=='anthropic')
        self.assertEqual(provider['state'],'stale');self.assertEqual(provider['models'],[])

    def test_invalid_catalog_does_not_return_untrusted_rows(self):
        (self.root/'models.json').write_text('{"schema":99}',encoding='utf-8')
        result=self.catalog.read()
        self.assertEqual(result['error'],'invalid_model_catalog')
        self.assertFalse(any(row['models'] for row in result['providers']))

    def test_image_connection_capabilities_do_not_expose_native_url_or_credentials(self):
        values = {row['id']: row for row in self.catalog.read()['providers']}
        self.assertEqual(values['openai']['image_protocol'], 'openai')
        self.assertTrue(values['openai']['image_configured'])
        self.assertEqual(values['gemini']['image_protocol'], 'gemini')
        self.assertFalse(values['gemini']['image_configured'])
        self.assertIsNone(values['anthropic']['image_protocol'])
        self.assertNotIn('base_url', str(values))

    def test_action_writes_version_bound_request_and_preserves_prompt(self):
        original=(self.root/'instance/private_demo/system.md').read_bytes()
        item=self.repository.get('private_demo')
        result=self.repository.request_instance('private_demo','stop',item['revision'])
        self.assertFalse(result['enabled'])
        from wxbridge.app.instance_control import read_request
        record=read_request(self.root/'instance/private_demo/.runtime-request.json')
        self.assertEqual(record['revision'],result['revision'])
        self.assertEqual(original,(self.root/'instance/private_demo/system.md').read_bytes())

    def test_failed_request_write_restores_config(self):
        item=self.repository.get('private_demo');write=self.repository._write
        def fail(path,data):
            if path.name=='.runtime-request.json':raise OSError('synthetic')
            return write(path,data)
        with patch.object(self.repository,'_write',side_effect=fail):
            with self.assertRaises(OSError):self.repository.request_instance('private_demo','stop',item['revision'])
        self.assertEqual(item['revision'],self.repository.get('private_demo')['revision'])
