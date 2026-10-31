"""Run separately: unittest discover -s tests/web (Windows needs local event-loop sockets)."""
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from wxbridge_web.app import create_app
from wxbridge_web.demo import DEMO_PASSWORD,DemoServices,seed


class HTTPTests(unittest.TestCase):
    def setUp(self):
        parent=Path(__file__).resolve().parents[2]/'.test-tmp';parent.mkdir(exist_ok=True)
        temp=tempfile.TemporaryDirectory(dir=parent);self.addCleanup(temp.cleanup)
        root=Path(temp.name);seed(root)
        self.app=create_app(root,DEMO_PASSWORD,services=DemoServices(root),demo=True,allowed_hosts=['testserver'])
        self.client=self.enterContext(TestClient(self.app))

    def login(self):
        response=self.client.post('/api/login',json={'password':DEMO_PASSWORD})
        self.assertEqual(response.status_code,200)
        return {'X-CSRF-Token':response.json()['csrf']}

    def test_authentication_and_secret_inputs_not_echoed(self):
        self.assertEqual(self.client.get('/api/instances').status_code,401)
        response=self.client.post('/api/login',json={'password':DEMO_PASSWORD,'api_key':'synthetic-private'})
        self.assertEqual(response.status_code,422)
        self.assertNotIn('synthetic-private',response.text)
        self.assertNotIn(DEMO_PASSWORD,response.text)

    def test_csrf_and_cross_origin_writes_rejected(self):
        headers=self.login()
        self.assertEqual(self.client.post('/api/services/start').status_code,403)
        self.assertEqual(self.client.post('/api/services/start',headers=dict(headers,Origin='https://untrusted.invalid')).status_code,403)

    def test_successful_edit_and_conflicting_edit(self):
        headers=self.login();item=self.client.get('/api/instances/private_demo').json()
        body=dict(config=dict(item['config'],enabled=False),revision=item['revision'])
        self.assertEqual(self.client.put('/api/instances/private_demo',json=body,headers=headers).status_code,200)
        self.assertEqual(self.client.put('/api/instances/private_demo',json=body,headers=headers).status_code,409)

    def test_prompt_roundtrip_and_protected_history_restore(self):
        headers=self.login();original=self.client.get('/api/instances/private_demo/prompt').json()
        payload={key:original[key] for key in ['revision','config_revision']};payload['text']='合成新提示词'
        result=self.client.put('/api/instances/private_demo/prompt',json=payload,headers=headers)
        self.assertEqual(result.status_code,200)
        current=result.json()
        response=self.client.post('/api/instances/private_demo/prompt/restore/'+current['history'][0]['id'],
            json={key:current[key] for key in ['revision','config_revision']},headers=headers)
        self.assertEqual(response.json()['text'],original['text'])

    def test_logout_invalidates_session(self):
        headers=self.login()
        self.assertEqual(self.client.delete('/api/session',headers=headers).status_code,200)
        self.assertEqual(self.client.get('/api/overview').status_code,401)

    def test_login_rate_limit(self):
        for _ in range(5):self.assertEqual(self.client.post('/api/login',json={'password':'wrong'}).status_code,401)
        self.assertEqual(self.client.post('/api/login',json={'password':'wrong'}).status_code,429)

    def test_fixed_action_validation_and_cookie_flags(self):
        response=self.client.post('/api/login',json={'password':DEMO_PASSWORD})
        self.assertIn('HttpOnly',response.headers['set-cookie'])
        self.assertIn('SameSite=strict',response.headers['set-cookie'])
        response=self.client.post('/api/services/arbitrary',headers={'X-CSRF-Token':response.json()['csrf']})
        self.assertEqual(response.status_code,400)

    def test_instance_enable_stop_and_runtime_models(self):
        headers=self.login()
        self.app.state.operations.services.perform('start')
        current=self.client.get('/api/overview').json()
        private=next(row for row in current['instances'] if row['id']=='private_demo')
        self.assertEqual(private['runtime_state'],'running')
        response=self.client.post('/api/instances/private_demo/actions/stop',json={'revision':private['revision']},headers=headers)
        self.assertEqual(response.status_code,202)
        stopped=next(row for row in self.client.get('/api/overview').json()['instances'] if row['id']=='private_demo')
        self.assertEqual(stopped['runtime_state'],'stopped')
        self.assertFalse(stopped['enabled'])

    def test_offline_enable_does_not_start_account_service(self):
        headers=self.login();item=self.client.get('/api/instances/group_demo').json()
        response=self.client.post('/api/instances/group_demo/actions/start',json={'revision':item['revision']},headers=headers)
        self.assertEqual(response.json()['state'],'waiting_service')
        self.assertFalse(self.app.state.operations.services.active)
        row=next(row for row in self.client.get('/api/overview').json()['instances'] if row['id']=='group_demo')
        self.assertEqual(row['runtime_state'],'waiting_service')

    def test_model_catalog_is_local_and_refresh_demo_never_calls_provider(self):
        headers=self.login()
        models=self.client.get('/api/models').json()
        self.assertTrue(any(provider['models'] for provider in models['providers']))
        response=self.client.post('/api/models/refresh',json={},headers=headers)
        self.assertEqual(response.json()['state'],'demo')
