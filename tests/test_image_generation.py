"""Offline SDK wire, queued generation, artifact and delivery contracts."""
from wxbridge.contracts.image_jobs import ImageProposal
from tests.support.configuration import replace_config, update_config, snapshot
import base64
from concurrent.futures import Future
from copy import deepcopy
import io
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import httpx
import httpx2
import openai
from google import genai
from PIL import Image

from provider import GeneratedImage, ImageRequest, ImageResult, ModelError, create_client, load_config
from provider.gemini_images import build_gemini_image_payload, parse_gemini_image_response
from tests.support.configuration import validate_models
from wxbridge.ai.reply.runner import run_tool_loop
from tests.support.tool_support import model_result
from wxbridge.app.tasks.image_generation import checked_image
from wxbridge.storage.image_artifacts import artifact_path
from tests.support.model_support import default_config
import tests.support.pipeline as pipeline
from tests.support.synthetic import msg


def picture(fmt='PNG'):
    buffer = io.BytesIO()
    Image.new('RGB', (4, 3), 'orange').save(buffer, format=fmt)
    return buffer.getvalue()


class ImageProviderTests(unittest.TestCase):
    def config(self, **environ):
        return load_config(Path(__file__).parent/'.synthetic-provider.env', environ={
            'GEMINI_API_KEY': 'synthetic-cpa-key', 'GEMINI_BASE_URL': 'http://127.0.0.1:8317/v1',
            'GEMINI_PROTOCOL': 'openai_compatible',
            'GEMINI_IMAGE_BASE_URL': 'http://127.0.0.1:8317', 'OPENAI_API_KEY': 'synthetic-gpt-key', **environ})

    def test_real_google_sdk_native_path_modalities_bytes_reuse_and_close(self):
        calls = []
        data = picture()
        def respond(request):
            calls.append(request)
            return httpx.Response(200, json=dict(candidates=[dict(content=dict(parts=[
                dict(text='PRIVATE_TEXT'), dict(inlineData=dict(mimeType='image/png', data=base64.b64encode(data).decode()))]))],
                usageMetadata=dict(promptTokenCount=5, candidatesTokenCount=6, totalTokenCount=11)))
        created = []
        constructor = genai.Client
        def make(**kwargs):
            kwargs['http_options']['client_args']['transport'] = httpx.MockTransport(respond)
            value = constructor(**kwargs)
            created.append(value)
            return value
        with patch.object(genai, 'Client', side_effect=make) as factory, patch('provider.client.create_sdk_client') as openai_factory:
            with create_client('gemini', config=self.config()) as client:
                for _ in range(2):
                    result = client.generate_images(ImageRequest('gemini-3.1-flash-image', 'PRIVATE_PROMPT'))
                    self.assertEqual(result.images[0].data, data)
                    self.assertEqual(result.images[0].mime, 'image/png')
                    self.assertEqual(result.text, 'PRIVATE_TEXT')
                self.assertEqual(result.usage['total_tokens'], 11)
                self.assertNotIn('PRIVATE', repr(result))
            factory.assert_called_once()
            openai_factory.assert_not_called()
        self.assertTrue(created[0]._api_client._httpx_client.is_closed)
        self.assertEqual(len(calls), 2)
        for call in calls:
            self.assertEqual(call.url.path, '/v1beta/models/gemini-3.1-flash-image:generateContent')
            self.assertEqual(call.headers['x-goog-api-key'], 'synthetic-cpa-key')
            body = json.loads(call.content)
            self.assertEqual(body['generationConfig']['responseModalities'], ['TEXT', 'IMAGE'])
            self.assertEqual(body['generationConfig']['imageConfig']['aspectRatio'], '1:1')
            self.assertNotIn('quality', body['generationConfig'])

    def test_gpt_keeps_official_images_endpoint_and_low_square_defaults(self):
        calls = []
        data = picture()
        def respond(request):
            calls.append(request)
            return httpx2.Response(200, json=dict(data=[dict(b64_json=base64.b64encode(data).decode())]))
        transport = openai.DefaultHttpxClient(transport=httpx2.MockTransport(respond))
        constructor = openai.OpenAI
        with patch.object(openai, 'OpenAI', side_effect=lambda **kw: constructor(**kw, http_client=transport)):
            with create_client('openai', config=self.config()) as client:
                result = client.generate_images(ImageRequest('gpt-image-1', 'synthetic'))
        self.assertEqual(result.images[0].data, data)
        self.assertEqual(calls[0].url.path, '/v1/images/generations')
        body = json.loads(calls[0].content)
        self.assertEqual((body['quality'], body['size']), ('low', '1024x1024'))
        self.assertNotIn('aspect_ratio', body)

    def test_google_sdk_does_not_retry_a_rejected_request_or_expose_error_body(self):
        calls = []
        def respond(request):
            calls.append(request)
            return httpx.Response(429, json=dict(error=dict(code=429, message='PRIVATE_ERROR', status='RESOURCE_EXHAUSTED')))
        constructor = genai.Client
        def make(**kwargs):
            kwargs['http_options']['client_args']['transport'] = httpx.MockTransport(respond)
            return constructor(**kwargs)
        with patch.object(genai, 'Client', side_effect=make):
            with create_client('gemini', config=self.config()) as client:
                with self.assertRaises(ModelError) as error:
                    client.generate_images(ImageRequest('synthetic-image', 'PRIVATE_PROMPT'))
        self.assertEqual(error.exception.code, 'http_429')
        self.assertNotIn('PRIVATE', str(error.exception))
        self.assertEqual(len(calls), 1)

    def test_protocol_options_do_not_leak_across_connections(self):
        for connection, options in [('gemini', dict(quality='low')), ('openai', dict(aspect_ratio='1:1'))]:
            with self.subTest(connection=connection), create_client(connection, config=self.config()) as client:
                with self.assertRaisesRegex(ModelError, 'invalid_image_options'):
                    client.generate_images(ImageRequest('synthetic', 'synthetic', options))
        cfg = default_config()
        cfg = replace_config(cfg, 'model_clients.image', 'gemini')
        cfg = replace_config(cfg, 'models.image.options', dict(quality='low'))
        with self.assertRaisesRegex(ValueError, 'invalid_image_options'):
            validate_models(cfg)

    def test_missing_native_url_and_expired_deadline_never_create_a_google_client(self):
        with patch.object(genai, 'Client') as factory:
            with create_client('gemini', config=self.config(GEMINI_IMAGE_BASE_URL='')) as client:
                with self.assertRaisesRegex(ModelError, 'missing_image_base_url'):
                    client.generate_images(ImageRequest('synthetic', 'synthetic'))
            with create_client('gemini', config=self.config()) as client:
                with self.assertRaisesRegex(ModelError, 'deadline_exceeded'):
                    client.generate_images(ImageRequest('synthetic', 'synthetic'), deadline=0)
            factory.assert_not_called()

    def test_rest_and_sdk_bytes_are_decoded_once_and_thought_images_are_excluded(self):
        data = picture()
        for blob in (data, base64.b64encode(data).decode()):
            response = dict(candidates=[dict(content=dict(parts=[dict(thought=True, inlineData=dict(mimeType='image/png',data='bad')),
                dict(inlineData=dict(mimeType='image/png',data=blob))]))])
            self.assertEqual(parse_gemini_image_response(response).images[0].data, data)
        for response in (dict(candidates=[]), dict(candidates=[dict(content=dict(parts=[dict(text='no image')]))])):
            with self.assertRaisesRegex(ModelError, 'empty_image_result'):
                parse_gemini_image_response(response)

    def test_decoder_rejects_spoofed_mime_and_normalizes_webp(self):
        data, metadata = checked_image(GeneratedImage(picture('WEBP'), mime='image/webp'))
        self.assertEqual(metadata['mime'], 'image/png')
        self.assertTrue(data.startswith(b'\x89PNG'))
        with self.assertRaisesRegex(ModelError, 'image_mime_mismatch'):
            checked_image(GeneratedImage(picture(), mime='image/jpeg'))
        with self.assertRaisesRegex(ModelError, 'image_bytes_required'):
            checked_image(GeneratedImage(url='https://example.invalid/private'))

    def test_image_tool_is_available_with_search_off_and_never_searches(self):
        cfg = default_config()
        cfg = replace_config(cfg, 'search.enabled', False)
        cfg = replace_config(cfg, 'image_generation.enabled', True)
        client = Mock()
        client.request.side_effect = [model_result(dict(content=None, tool_calls=[dict(id='one', function=dict(
            name='generate_image', arguments=json.dumps(dict(prompt='PRIVATE_DRAWING'))))])), model_result(dict(content=''))]
        with patch('wxbridge.ai.tools.web.SearchClient') as search:
            result = run_tool_loop(cfg, client, [dict(role='user',content='画一只猫')])
        self.assertEqual(result.image_proposals, (ImageProposal(prompt='PRIVATE_DRAWING'),))
        self.assertNotIn('成功', result.text)
        self.assertFalse(result.search['attempted'])
        search.assert_not_called()
        tools = client.request.call_args_list[0].kwargs['tools']
        self.assertEqual([t['function']['name'] for t in tools], ['generate_image'])

    def test_image_tool_coexists_with_hosted_search_and_returns_its_tool_result(self):
        from provider import ModelResult
        cfg = default_config()
        cfg = replace_config(cfg, 'search.backend', 'native')
        cfg = replace_config(cfg, 'image_generation.enabled', True)
        client = Mock()
        client.request.side_effect = [ModelResult(dict(content='', tool_calls=[dict(id='one', function=dict(
            name='generate_image', arguments='{"prompt":"synthetic drawing"}'))]), 'tool_calls', {}, {}),
            model_result(dict(content=''))]
        result = run_tool_loop(cfg, client, [dict(role='user', content='画一只猫')])
        self.assertEqual(result.image_proposals, (ImageProposal(prompt='synthetic drawing'),))
        self.assertEqual(client.request.call_count, 2)
        self.assertIn('native_search', client.request.call_args_list[0].kwargs)
        self.assertEqual(client.request.call_args_list[0].kwargs['tools'][0]['function']['name'], 'generate_image')


class ImagePipelineTests(unittest.TestCase):
    setUp = pipeline.PipelineFixture.setUp
    make_bot = pipeline.PipelineFixture.make_bot
    add = pipeline.PipelineFixture.add

    def test_confirmed_image_reappears_in_later_reply_and_survives_restart(self):
        from wxbridge.ai.reply.images import prepare_multimodal
        self.cfg = replace_config(self.cfg, 'vision.context_images_enabled', True, owner=self)
        task = self.ready_image()
        self.future.set_result(ImageResult((GeneratedImage(picture(), mime='image/png'),)))
        self.bot.image_generation.step()
        image = self.bot.journal.task(task['id'])['images'][0]
        path = artifact_path(self.cfg.paths.queue_file, image)
        self.assertEqual(self.bot.journal.context_images(self.now), [])
        with self.bot.journal.db:
            parent = self.bot.journal.task(task['parent_task_id'])
            parent.update(phase='done', chunks=[], triggers=[])
            self.bot.journal.put(parent)
        self.bot._send_step()
        self.jobs[-1].result = dict(ok=True, ui=dict(operation=dict(submitted=True)))
        self.bot._ui_result_step()
        self.rows.append(dict(msg(2, int(self.now), '', 'wxid_self'), type=3, image_md5=image['md5']))
        self.bot._confirm_step()
        self.assertEqual(len(self.bot.journal.context_images(self.now)), 1)
        self.now += 31
        self.bot.image_generation.cleanup(self.now)
        self.assertTrue(path.exists())
        self.add(3, text='@DeepSeek 刚才那张图是什么？')
        self.bot.tick()
        job = next(j for j in reversed(self.jobs) if j.kind == 'generate')
        self.assertEqual(job.payload[3]['images'][0]['generated_image'], image)
        with patch('wechat_linux.media.images.read_local_image', side_effect=AssertionError('no cache read')):
            _, info = prepare_multimodal(self.cfg, job.payload[1], job.payload[3])
        self.assertEqual(info['images'], 1)
        self.bot.close()
        self.assertTrue(path.exists())
        with patch('wxbridge.submission_api.server.SubmissionServer'):
            self.make_bot()
        self.assertEqual(len(self.bot.journal.context_images(self.now)), 1)
        self.bot.image_generation.cleanup(self.now+self.cfg.target.context_max_age_seconds+1, force=True)
        self.assertFalse(path.exists())

    def ready_image(self):
        self.cfg = replace_config(self.cfg, 'image_generation.enabled', True, owner=self)
        self.cfg = replace_config(self.cfg, 'account.ui.backend', 'native_service', owner=self)
        self.cfg = update_config(self.cfg, 'account.helper_transport', enabled=True, allowed_chat_ids=[self.bot.chat_id], owner=self)
        self.future = Future()
        self.future.cancel_request = Mock(side_effect=self.future.cancel)
        self.bot.submit_images = Mock(return_value=self.future)
        self.add(1)
        self.bot.tick()
        worker = next(job for job in self.jobs if job.kind == 'generate')
        worker.result = dict(ok=True, reply='', image_proposals=[ImageProposal(prompt='PRIVATE_DRAWING')])
        self.bot.replies.collect()
        image = self.bot.journal.image_tasks()[0]
        self.bot.image_generation.step()
        return image

    def test_owner_adds_confirmation_only_to_an_accepted_image_proposal(self):
        image = self.ready_image()
        parent = self.bot.journal.task(image['parent_task_id'])
        self.assertEqual(parent['chunks'], ['在画了，稍后发给你'])
        self.assertEqual(len(self.bot.journal.image_tasks()), 1)

    def test_full_queue_rejects_proposal_without_a_success_confirmation(self):
        self.cfg = replace_config(self.cfg, 'image_generation.enabled', True, owner=self)
        self.cfg = replace_config(self.cfg, 'queue.max_pending', 1, owner=self)
        self.add(1)
        self.bot.tick()
        worker = next(job for job in self.jobs if job.kind == 'generate')
        worker.result = dict(ok=True, reply='', image_proposals=[ImageProposal(prompt='cat')])
        self.bot.replies.collect()
        self.assertEqual(self.bot.journal.image_tasks(), [])
        self.assertNotIn('在画了', json.dumps(self.bot.journal.tasks(), ensure_ascii=False))

    def test_late_parent_version_discards_image_proposal_and_confirmation(self):
        self.cfg = replace_config(self.cfg, 'image_generation.enabled', True, owner=self)
        self.add(1)
        self.bot.tick()
        worker = next(job for job in self.jobs if job.kind == 'generate')
        parent = self.bot.journal.head()
        with self.bot.journal.db:
            parent['version'] += 1
            self.bot.journal.put(parent)
        worker.result = dict(ok=True, reply='', image_proposals=[ImageProposal(prompt='cat')])
        self.bot.replies.collect()
        self.assertTrue(worker.closed)
        self.assertEqual(self.bot.journal.image_tasks(), [])
        self.assertNotIn('在画了', json.dumps(self.bot.journal.tasks(), ensure_ascii=False))

    def test_slow_image_does_not_block_new_foreground_generation_and_uses_snapshot(self):
        task = self.ready_image()
        self.assertEqual(self.bot.journal.task(task['id'])['generation'], 'image_running')
        request = self.bot.submit_images.call_args.args[0]
        self.cfg = replace_config(self.cfg, 'models.image.model', 'changed-after-enqueue', owner=self)
        self.assertNotEqual(request.model, self.cfg.models.image.model)
        self.add(2)
        self.bot.tick()
        self.assertEqual(len([j for j in self.jobs if j.kind == 'generate']), 2)
        self.assertNotEqual(self.bot.journal.head()['id'], task['id'])
        self.assertNotIn('base64', json.dumps(self.bot.journal.tasks()))

    def test_result_reaches_native_image_submission_and_only_matching_outbound_confirms(self):
        task = self.ready_image()
        self.future.set_result(ImageResult((GeneratedImage(picture(), mime='image/png'),)))
        self.bot.image_generation.step()
        image = self.bot.journal.task(task['id'])['images'][0]
        path = artifact_path(self.cfg.paths.queue_file, image)
        self.assertTrue(path.is_file())
        parent = self.bot.journal.task(task['parent_task_id'])
        with self.bot.journal.db:
            parent.update(phase='done', chunks=[], triggers=[])
            self.bot.journal.put(parent)
        self.bot._send_step()
        send = self.jobs[-1]
        self.assertEqual(send.kind, 'native_send_image')
        self.assertEqual(send.payload[2]['sha256'], image['sha256'])
        send.result = dict(ok=True, ui=dict(operation=dict(submitted=True)))
        self.bot._ui_result_step()
        wrong = dict(msg(2, int(self.now), '', 'wxid_self'), type=3, image_md5='b'*32)
        self.rows.append(wrong)
        self.bot._confirm_step()
        self.assertEqual(self.bot.journal.task(task['id'])['phase'], 'queued')
        self.rows.append(dict(wrong, local_id=3, image_md5=image['md5']))
        self.bot._confirm_step()
        self.assertEqual(self.bot.journal.task(task['id'])['phase'], 'done')
        self.bot.image_generation.cleanup(float('inf'))
        self.assertFalse(path.exists())

    def test_expired_result_is_discarded_without_artifacts_or_second_request(self):
        task = self.ready_image()
        self.now = task['image_deadline']+1
        self.future.set_result(ImageResult((GeneratedImage(picture()),)))
        self.bot.image_generation.step()
        self.assertEqual(self.bot.journal.task(task['id'])['phase'], 'done')
        self.bot.submit_images.assert_called_once()
        self.assertFalse((Path(self.cfg.paths.queue_file).parent/'generated-images').exists())

    def test_ready_expired_image_does_not_block_later_text_or_keep_artifact(self):
        task = self.ready_image()
        self.future.set_result(ImageResult((GeneratedImage(picture()),)))
        self.bot.image_generation.step()
        task = self.bot.journal.task(task['id'])
        path = artifact_path(self.cfg.paths.queue_file, task['images'][0])
        self.now = task['reply_expires_at']+1
        self.bot.image_generation.step()
        self.bot.image_generation.cleanup(float('inf'))
        self.assertEqual(self.bot.journal.task(task['id'])['phase'], 'done')
        self.assertFalse(path.exists())

    def test_crash_recovery_marks_running_request_failed_without_regeneration(self):
        task = self.ready_image()
        # Recover the durable record without a graceful controller shutdown.
        self.bot.journal.recover(self.now+1)
        recovered = self.bot.journal.task(task['id'])
        self.assertEqual(recovered['image_error'], 'image_generation_interrupted')
        self.assertEqual(recovered['generation'], 'ready')
        self.assertNotIn('image_request', recovered)
        self.bot.image_generation.jobs.clear()
        self.bot.image_generation.step()
        self.bot.submit_images.assert_called_once()

    def test_disabling_discards_a_ready_image_before_submission(self):
        task = self.ready_image()
        self.future.set_result(ImageResult((GeneratedImage(picture()),)))
        self.bot.image_generation.step()
        task = self.bot.journal.task(task['id'])
        path = artifact_path(self.cfg.paths.queue_file, task['images'][0])
        self.cfg = replace_config(self.cfg, 'image_generation.enabled', False, owner=self)
        self.bot.image_generation.step()
        self.bot.image_generation.cleanup(float('inf'))
        self.assertEqual(self.bot.journal.task(task['id'])['phase'], 'done')
        self.assertFalse(path.exists())

    def test_shutdown_cancels_job_and_foreign_identity_is_never_applied(self):
        task = self.ready_image()
        with self.bot.journal.db:
            value = self.bot.journal.task(task['id'])
            value['image_identity']['chat'] = 'other@chatroom'
            self.bot.journal.put(value)
        self.bot.image_generation.step()
        self.future.cancel_request.assert_called_once()
        self.assertEqual(self.bot.journal.task(task['id'])['phase'], 'done')


if __name__ == '__main__':
    unittest.main()
