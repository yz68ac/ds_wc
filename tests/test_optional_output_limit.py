"""An explicit null bypasses inherited output limits only on optional-limit APIs."""
from wxbridge.ai.roles import provider_settings
from tests.support.configuration import replace_config, update_config, snapshot
from copy import deepcopy
from wxbridge.config import json_value
import unittest
from unittest.mock import patch

from tests.support.model_support import ClientFixture, default_config
from provider import ModelError, ModelRequest
from provider.adapters import get_protocol_adapter
from wxbridge.ai.roles import model_settings
from tests.support.configuration import validate_models
from tests.support.configuration import merge_config as deep_merge


class OptionalOutputLimitTests(unittest.TestCase):
    def test_null_survives_default_merge_and_is_absent_from_deepseek_request(self):
        cfg=deep_merge(default_config(), {'models': {'participation': {'options': {'max_tokens': None}}}})
        validate_models(cfg)
        settings=provider_settings(model_settings(cfg,'participation'))
        self.assertIsNone(settings['max_tokens'])
        with ClientFixture(cfg,role='participation') as client:
            response={'choices':[{'message':{'content':'{"reply_to":[]}'},'finish_reason':'stop'}]}
            with patch.object(client,'_post',return_value=response) as post:
                client.generate(ModelRequest([{'role':'user','content':'synthetic'}],model='synthetic',
                    options={'max_tokens':None}),require_complete=True)
        self.assertNotIn('max_tokens',post.call_args.args[0])
        self.assertEqual(json_value(default_config().models)['participation']['options']['max_tokens'],256)

    def test_compatible_payload_preserves_explicit_and_existing_default_limits(self):
        request=ModelRequest([{'role':'user','content':'synthetic'}])
        for protocol in ('deepseek','openai_compatible'):
            adapter=get_protocol_adapter(protocol)
            for limit in (None,256):
                settings={'model':'synthetic','max_tokens':limit,'thinking':'enabled','reasoning_effort':'max'}
                wire=adapter.build_payload(settings,request)
                self.assertEqual('max_tokens' in wire,limit is not None)
                if limit is not None:self.assertEqual(wire['max_tokens'],limit)
                self.assertEqual(settings['max_tokens'],limit)
            self.assertEqual(adapter.build_payload({'model':'synthetic'},request)['max_tokens'],1200)

    def test_required_limit_protocol_and_invalid_numbers_still_reject(self):
        cfg=default_config()
        cfg = replace_config(cfg, 'model_clients.reply', 'anthropic')
        cfg = replace_config(cfg, 'models.reply.options.max_tokens', None)
        with self.assertRaisesRegex(ValueError,'invalid_model_option'):
            validate_models(cfg)
        with ClientFixture(cfg) as client:
            with patch.object(client,'_post') as post:
                with self.assertRaisesRegex(ModelError,'invalid_model_options'):
                    client.generate(ModelRequest([],model='synthetic',options={'max_tokens':None}))
                post.assert_not_called()
        for limit in (0,True,1.5):
            cfg=deepcopy(default_config())
            with self.assertRaises(ValueError):
                cfg = replace_config(cfg, 'models.participation.options.max_tokens', limit)
                validate_models(cfg)


if __name__=='__main__':
    unittest.main()
