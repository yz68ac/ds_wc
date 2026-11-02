"""Run separately: unittest discover -s tests/web (Windows needs local event-loop sockets)."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from web.backend.app import create_app
from web.backend.demo import DEMO_PASSWORD,DemoServices,seed


class HTTPTests(unittest.TestCase):
    def test_contacts_search_supports_remarks_groups_and_existing_bindings(self):
        self.assertEqual(self.client.get('/api/contacts').status_code,401)
        self.login()
        rows=self.client.get('/api/contacts',params={'q':'小 林'}).json()['items']
        self.assertEqual(rows[0]['id'],'wxid_synthetic_friend')
        self.assertEqual(rows[0]['instance_id'],'private_demo')
        self.assertNotIn('summary',rows[0])
        groups=self.client.get('/api/contacts',params={'q':'开发','kind':'group'}).json()['items']
        self.assertEqual(groups[0]['id'],'123456789@chatroom')
        self.assertEqual(self.client.get('/api/contacts',params={'q':'x'*129}).status_code,400)


    def test_configuration_schema_and_account_edits_require_auth_and_csrf(self):
        self.assertEqual(self.client.get('/api/config/schema').status_code,401)
        self.assertEqual(self.client.get('/api/account/settings').status_code,401)
        headers=self.login()
        schema=self.client.get('/api/config/schema').json()
        self.assertGreater(len(schema['groups']),5)
        settings=self.client.get('/api/account/settings').json()
        body={'values':{'poll_interval_seconds':4},'revision':settings['revision']}
        self.assertEqual(self.client.put('/api/account/settings',json=body).status_code,403)
        saved=self.client.put('/api/account/settings',json=body,headers=headers)
        self.assertEqual(saved.status_code,200)
        self.assertEqual(saved.json()['values']['poll_interval_seconds'],4)
        self.assertFalse(self.app.state.operations.services.active)
        self.assertEqual(self.client.put('/api/account/settings',json=body,headers=headers).status_code,409)
        invalid={'values':{'native_api.allowed_chat_ids':['wxid_other']},'revision':saved.json()['revision']}
        self.assertEqual(self.client.put('/api/account/settings',json=invalid,headers=headers).status_code,400)

    def test_onboarding_overview_reports_only_configured_authorizations(self):
        self.login()
        snapshot=self.client.get('/api/overview').json()
        setup=snapshot['account_setup']
        self.assertEqual(setup['instance_count'],2)
        self.assertEqual(set(setup['pending_instances']),{'private_demo','group_demo'})
        self.assertEqual(setup['authorized_count'],0)
        self.assertTrue(setup['can_bind'])
        self.assertNotIn('db_dir',setup)

    def test_demo_binding_updates_authorizations_without_starting_bot(self):
        headers=self.login()
        self.assertEqual(self.client.post('/api/account/bind',headers=headers).status_code,200)
        snapshot=self.client.get('/api/overview').json()
        self.assertTrue(snapshot['account_setup']['bound'])
        self.assertEqual(snapshot['account_setup']['pending_instances'],[])
        self.assertEqual(snapshot['account_setup']['authorized_count'],2)
        self.assertFalse(self.app.state.operations.services.active)

    def test_account_onboarding_requires_session_csrf_and_same_origin(self):
        services=self.app.state.operations.services
        with patch.object(services,'onboarding',create=True,return_value={'phase':'qr_required','qr':'synthetic'}) as action:
            self.assertEqual(self.client.post('/api/account/login').status_code,401)
            headers=self.login()
            self.assertEqual(self.client.post('/api/account/login').status_code,403)
            self.assertEqual(self.client.post('/api/account/login',headers=dict(headers,Origin='https://untrusted.invalid')).status_code,403)
            action.assert_not_called()
            response=self.client.post('/api/account/login',headers=headers)
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.headers['cache-control'],'no-store')
            action.assert_called_once_with('login')

    def setUp(self):
        parent=Path(__file__).resolve().parents[2]/'.test-tmp';parent.mkdir(exist_ok=True)
        temp=tempfile.TemporaryDirectory(dir=parent);self.addCleanup(temp.cleanup)
        root=Path(temp.name);seed(root)
        self.app=create_app(root,DEMO_PASSWORD,services=DemoServices(root),demo=True,allowed_hosts=['testserver'],health_token='synthetic-health-token')
        self.client=self.enterContext(TestClient(self.app))

    def login(self):
        response=self.client.post('/api/login',json={'password':DEMO_PASSWORD})
        self.assertEqual(response.status_code,200)
        return {'X-CSRF-Token':response.json()['csrf']}

    def test_health_is_public_and_contains_no_private_state(self):
        response=self.client.get('/healthz')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.content,b'{"status":"ok"}')
        self.assertEqual(response.headers['x-wxbridge-health'],'synthetic-health-token')
        self.assertEqual(response.headers['cache-control'],'no-store')

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
