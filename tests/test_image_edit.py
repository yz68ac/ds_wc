"""Offline edit transports, quote-only tools and private source snapshots."""
from wxbridge.contracts.image_jobs import ImageProposal
from wxbridge.contracts.image_jobs import QuotedImageInput
import base64
from concurrent.futures import Future
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import httpx
import httpx2
import openai
from google import genai

from provider import ImageInput, ImageRequest, ImageResult, GeneratedImage, ModelError, create_client
from provider.images import build_image_edit_payload
from tests.support import media, pipeline
from tests.support.configuration import replace_config
from tests.support.synthetic import msg
from tests.support.tool_support import model_result
import tests.test_image_generation as image_tests
from wxbridge.ai.composer import reference_source
from wxbridge.ai.reply.runner import run_tool_loop
from wxbridge.app.execution.model_runtime import ConversationModels
from wxbridge.storage.image_artifacts import read_artifact, write_artifact
from wxbridge.app.tasks.image_generation import checked_image

picture = image_tests.picture

class EditProviderTests(unittest.TestCase):
    config = image_tests.ImageProviderTests.config

    def test_openai_sdk_edits_multipart_contains_original_bytes(self):
        calls, data = [], picture()
        def respond(request):
            calls.append(request)
            return httpx2.Response(200, json=dict(data=[dict(b64_json=base64.b64encode(data).decode())]))
        transport = openai.DefaultHttpxClient(transport=httpx2.MockTransport(respond))
        constructor = openai.OpenAI
        with patch.object(openai, 'OpenAI', side_effect=lambda **kw: constructor(**kw, http_client=transport)):
            with create_client('openai', config=self.config()) as client:
                result = client.generate_images(ImageRequest('synthetic-edit-model', 'change background',
                    images=(ImageInput(data),)))
        self.assertEqual(result.images[0].data, data)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].url.path, '/v1/images/edits')
        self.assertIn('multipart/form-data', calls[0].headers['content-type'])
        self.assertIn(data, calls[0].content)
        self.assertIn(b'change background', calls[0].content)

    def test_gemini_sdk_includes_inline_source_image_with_edit_prompt(self):
        calls, data = [], picture()
        def respond(request):
            calls.append(request)
            return httpx.Response(200, json=dict(candidates=[dict(content=dict(parts=[
                dict(inlineData=dict(mimeType='image/png', data=base64.b64encode(data).decode()))]))]))
        constructor = genai.Client
        def make(**kwargs):
            kwargs['http_options']['client_args']['transport'] = httpx.MockTransport(respond)
            return constructor(**kwargs)
        with patch.object(genai, 'Client', side_effect=make):
            with create_client('gemini', config=self.config()) as client:
                result = client.generate_images(ImageRequest('synthetic-edit-model', 'change background',
                    images=(ImageInput(data),)))
        self.assertEqual(result.images[0].data, data)
        parts = json.loads(calls[0].content)['contents'][0]['parts']
        self.assertEqual(parts[0]['text'], 'change background')
        self.assertEqual(base64.b64decode(parts[1]['inlineData']['data']), data)
        blob = parts[1]['inlineData']
        self.assertEqual(blob.get('mimeType', blob.get('mime_type')), 'image/png')

    def test_rejected_edit_never_retries_or_falls_back_to_generation(self):
        calls = []
        def respond(request):
            calls.append(request)
            return httpx2.Response(400, json={'error': {'message': 'PRIVATE_SOURCE', 'type': 'invalid_request_error'}})
        transport = openai.DefaultHttpxClient(transport=httpx2.MockTransport(respond))
        constructor = openai.OpenAI
        with patch.object(openai, 'OpenAI', side_effect=lambda **kw: constructor(**kw, http_client=transport)):
            with create_client('openai', config=self.config()) as client:
                with self.assertRaises(ModelError) as error:
                    client.generate_images(ImageRequest('synthetic', 'change', images=(ImageInput(picture()),)))
        self.assertEqual(error.exception.code, 'http_400')
        self.assertNotIn('PRIVATE_SOURCE', str(error.exception))
        self.assertEqual([call.url.path for call in calls], ['/v1/images/edits'])

    def test_invalid_source_and_generation_only_options_are_rejected(self):
        for images in ((), (ImageInput(b'not an image'),), (ImageInput(picture(), 'image/jpeg'),)):
            with self.subTest(images=repr(images)), self.assertRaises(ModelError):
                build_image_edit_payload(ImageRequest('synthetic', 'change', images=images))
        with self.assertRaisesRegex(ModelError, 'invalid_image_options'):
            build_image_edit_payload(ImageRequest('synthetic', 'change', dict(style='natural'), (ImageInput(picture()),)))
        self.assertNotIn('PRIVATE', repr(ImageInput(b'PRIVATE')))


class EditToolTests(unittest.TestCase):
    setUp = media.LocalImageFixture.setUp
    generation = media.LocalImageFixture.generation
    prepare = media.LocalImageFixture.prepare

    def prompt(self):
        self.cfg = replace_config(self.cfg, 'image_generation.enabled', True)
        self.cfg = replace_config(self.cfg, 'search.enabled', False)
        self.path.write_bytes(picture())
        return self.prepare(self.generation())[0]

    def client(self, label='引用图片1'):
        client = Mock()
        client.request.side_effect = [model_result(dict(content='', tool_calls=[dict(id='edit', function=dict(
            name='edit_image', arguments=json.dumps(dict(image=label, prompt='把背景改成雪山'))))])),
            model_result(dict(content='请重新引用图片'))]
        return client

    def test_edit_tool_returns_only_selected_actual_quote_and_source_slot(self):
        prompt = self.prompt()
        client = self.client()
        result = run_tool_loop(self.cfg, client, prompt)
        self.assertEqual(len(result.image_proposals), 1)
        source = result.image_proposals[0].reference_image
        self.assertEqual(source.data, picture())
        self.assertEqual(source.trigger_index, 0)
        self.assertEqual(source.depth, 0)
        self.assertEqual([t['function']['name'] for t in client.request.call_args_list[0].kwargs['tools']],
                         ['generate_image', 'edit_image'])
        self.assertNotIn('edit_images', json.dumps(prompt))

    def test_missing_or_unknown_quote_cannot_prepare_an_edit(self):
        prompt = self.prompt()
        result = run_tool_loop(self.cfg, self.client('不存在的图片'), prompt)
        self.assertEqual(result.image_proposals, ())
        self.assertIn('image_edit_source_unavailable', result.search['errors'])
        self.path.unlink()
        prompt, _ = self.prepare(self.generation())
        client = self.client()
        result = run_tool_loop(self.cfg, client, prompt)
        self.assertEqual(result.image_proposals, ())
        self.assertNotIn('edit_image', [t['function']['name'] for t in client.request.call_args_list[0].kwargs['tools']])

    def test_plain_history_picture_is_not_an_edit_source(self):
        from wxbridge.ai.context.images import image_candidates
        from wxbridge.ai.reply.images import prepare_multimodal
        from wxbridge.ai.reply.prompt import build_prompt
        self.cfg = replace_config(self.cfg, 'image_generation.enabled', True)
        self.cfg = replace_config(self.cfg, 'vision.context_images_enabled', True)
        self.path.write_bytes(picture())
        row = dict(msg(1, 1700000500, ''), type=3, image_md5='a'*32)
        trigger = msg(2, 1700000600, '看一下')
        base = build_prompt(self.cfg, [row], trigger, '合成群')
        candidate = image_candidates(self.cfg, [row], [trigger], base)[0]
        prompt, _ = prepare_multimodal(self.cfg, base, dict(context=[row], triggers=[trigger], chat_title='合成群',
            images=[dict(self.binding, targets=candidate['targets'])]))
        self.assertEqual(prompt.edit_images, {})

    def test_generation_and_edit_share_one_task_limit(self):
        prompt = self.prompt()
        client = self.client()
        # Both calls occur in one batch: the second must not prepare another task.
        calls = [dict(id='edit', function=dict(name='edit_image', arguments=json.dumps(dict(image='引用图片1', prompt='改成蓝色')))),
                 dict(id='generate', function=dict(name='generate_image', arguments='{"prompt":"new image"}'))]
        client.request.side_effect = [model_result(dict(content='', tool_calls=calls)), model_result(dict(content=''))]
        result = run_tool_loop(self.cfg, client, prompt)
        self.assertEqual(len(result.image_proposals), 1)
        self.assertIn('image_tool_limit', result.search['errors'])


class EditPipelineTests(unittest.TestCase):
    setUp = pipeline.PipelineFixture.setUp
    make_bot = pipeline.PipelineFixture.make_bot
    add = pipeline.PipelineFixture.add

    def source(self, trigger):
        return QuotedImageInput(picture(), 'image/png', 0, 0, reference_source(trigger['reference']))

    def ready_edit(self):
        self.cfg = replace_config(self.cfg, 'image_generation.enabled', True, owner=self)
        trigger = self.add(1)
        trigger['reference'] = dict(kind='image', sender='wxid_friend', server_id='123', image_md5='a'*32, available=False)
        self.store.image_binding.return_value = dict(error='local_image_index_missing', candidates=[])
        self.bot.tick()
        worker = next(job for job in self.jobs if job.kind == 'generate')
        worker.result = dict(ok=True, reply='', image_proposals=[ImageProposal(prompt='改成蓝色', reference_image=self.source(trigger))])
        self.bot.replies.collect()
        return self.bot.journal.image_tasks()[0]

    def test_edit_snapshot_survives_queue_wait_but_pixels_never_enter_journal(self):
        task = self.ready_edit()
        source = task['image_request']['source_image']
        path, data = read_artifact(self.cfg.paths.queue_file, source)
        self.assertEqual(data, picture())
        self.assertIn('在改了', self.bot.journal.task(task['parent_task_id'])['chunks'][0])
        self.assertNotIn('reference_image', json.dumps(self.bot.journal.tasks()))
        self.assertNotIn('base64', json.dumps(self.bot.journal.tasks()))
        self.bot.image_generation.cleanup(self.now+31, force=True)
        self.assertTrue(path.exists())
        future = Future()
        self.bot.submit_images = Mock(return_value=future)
        self.bot.image_generation.step()
        self.assertEqual(self.bot.submit_images.call_args.kwargs['source_image'], source)
        self.assertEqual(self.bot.submit_images.call_args.args[0].prompt, '改成蓝色')
        self.now = task['image_deadline']+1
        self.bot.image_generation.step()
        self.bot.image_generation.cleanup(self.now, force=True)
        self.assertFalse(path.exists())

    def test_stale_or_forged_quote_is_rejected_before_writing_source(self):
        self.cfg = replace_config(self.cfg, 'image_generation.enabled', True, owner=self)
        trigger = self.add(1)
        trigger['reference'] = dict(kind='image', image_md5='a'*32)
        source = self.source(trigger)
        trigger['reference']['image_md5'] = 'b'*32
        with self.assertRaisesRegex(ModelError, 'invalid_image_edit_source'):
            self.bot.image_generation.proposal([ImageProposal('change', reference_image=source)], triggers=[trigger])
        self.assertFalse((Path(self.cfg.paths.queue_file).parent/'generated-images').exists())

    def test_private_quote_without_group_id_is_valid_and_rejected_proposal_is_cleaned(self):
        self.cfg = replace_config(self.cfg, 'image_generation.enabled', True, owner=self)
        trigger = self.add(1)
        trigger['reference'] = dict(kind='image', image_md5='a'*32, chat_id='')
        proposal = self.bot.image_generation.proposal([ImageProposal('change', reference_image=self.source(trigger))],
                                                    triggers=[trigger])
        path = read_artifact(self.cfg.paths.queue_file, proposal.source_image)[0]
        self.assertTrue(path.exists())
        self.bot.image_generation.discard_proposal(proposal)
        self.assertFalse(path.exists())

    def test_failed_edit_reports_failure_without_retrying_or_losing_source_cleanup(self):
        task = self.ready_edit()
        path = read_artifact(self.cfg.paths.queue_file, task['image_request']['source_image'])[0]
        future = Future()
        self.bot.submit_images = Mock(return_value=future)
        self.bot.image_generation.step()
        future.set_exception(ModelError('unsupported_image_edit'))
        self.bot.image_generation.step()
        self.bot.image_generation.cleanup(self.now, force=True)
        task = self.bot.journal.task(task['id'])
        self.assertEqual(task['chunks'], ['这次没改好，稍后再试吧'])
        self.assertFalse(path.exists())
        self.bot.submit_images.assert_called_once()

    def test_quoted_bot_output_uses_local_source_even_when_auto_context_is_off(self):
        self.cfg = replace_config(self.cfg, 'image_generation.enabled', True, owner=self)
        data, metadata = checked_image(GeneratedImage(picture(), mime='image/png'))
        image = write_artifact(self.cfg.paths.queue_file, data, metadata)
        original = dict(local_id=1, sender='wxid_self', image_md5=image['md5'], server_id='123', time=int(self.now))
        with self.bot.journal.db:self.bot.journal.remember_image(1, image, self.now, 60)
        trigger = self.add(2)
        trigger['reference'] = dict(kind='image', sender='wxid_self', image_md5=image['md5'], server_id='123', available=False)
        self.store.image_binding.return_value = dict(error='local_image_index_missing', source_message=original)
        self.bot.tick()
        job = next(job for job in self.jobs if job.kind == 'generate')
        self.assertEqual(job.payload[3]['images'][0]['generated_image'], image)
        self.assertFalse(self.cfg.vision.context_images_enabled)

    def test_image_lane_loads_snapshot_before_call_and_detects_tampering(self):
        data, metadata = checked_image(GeneratedImage(picture(), mime='image/png'))
        source = write_artifact(self.cfg.paths.queue_file, data, metadata)
        client = Mock()
        client.generate_images.return_value = ImageResult((GeneratedImage(picture(), mime='image/png'),))
        with patch('wxbridge.app.execution.model_runtime.create_client', return_value=client):
            models = ConversationModels(self.cfg)
            try:
                request = ImageRequest('synthetic-edit', 'change')
                models.submit_images(request, source_image=source).result(timeout=3)
                sent = client.generate_images.call_args.args[0]
                self.assertEqual(sent.images, (ImageInput(picture()),))
                self.assertEqual(request.images, ())
                client.reset_mock()
                path = read_artifact(self.cfg.paths.queue_file, source)[0]
                path.write_bytes(b'tampered')
                with self.assertRaisesRegex(ModelError, 'image_artifact_changed'):
                    models.submit_images(request, source_image=source).result(timeout=3)
                client.generate_images.assert_not_called()
            finally:models.close()
