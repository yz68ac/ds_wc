"""Synthetic bot-to-native image routing, recovery and reader ownership tests."""
import base64
from copy import deepcopy
import unittest
from unittest.mock import patch

from wechat_linux import ImageDownloadError
from wechat_linux.errors import MediaError
from wechat_linux.models import Image
from wechat_linux.native.image_download import NativeImageDownloader
import test_context_pipeline as pipeline
import test_media as media
import test_native_images as native


class NativeImageBotTests(unittest.TestCase):
    def setUp(self):
        self.case = media.LocalImageTests('runTest')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.case.cfg['vision'].update(download_enabled=True, download_backend='native')
        self.case.cfg['native_api'].update(helper_socket='/run/synthetic/helper.sock',
                                            allowed_chat_ids=[native.CHAT])
        self.generation = self.case.generation()
        self.generation['account'] = self.case.binding['account']
        self.generation['images'][0].update(
            download=dict(verified=True, chat_id=native.CHAT, server_id='123',
                          sender='wxid_person', image_md5='a'*32),
            native_request=dict(request_id='bot-image:synthetic', resume_only=False))
        self.thumb = dict(mime='image/png', width=10, height=20, bytes=8, quality='thumbnail')
        self.full = dict(mime='image/png', width=800, height=1200, bytes=8, quality='cached',
                         download=dict(backend='native', outcome='cache_verified'))

    def test_bound_native_download_reaches_model_without_desktop_or_rebinding(self):
        original = deepcopy(self.generation)
        with patch('wechat_linux.media.images.read_local_image', return_value=(b'thumb', self.thumb)), \
             patch.object(NativeImageDownloader, 'download', return_value=Image(b'full', self.full)) as download, \
             patch('wechat_linux.desktop.image_download.improve_image') as desktop:
            prompt, info = self.case.prepare(self.generation)
        desktop.assert_not_called()
        download.assert_called_once()
        call = download.call_args
        self.assertEqual(call.args[0]['download'], original['images'][0]['download'])
        self.assertEqual(call.kwargs['chat_id'], native.CHAT)
        self.assertEqual(call.kwargs['request_id'], 'bot-image:synthetic')
        self.assertFalse(call.kwargs['resume_only'])
        image_block = prompt[1]['content'][-1]['image_url']['url']
        self.assertEqual(base64.b64decode(image_block.split(',', 1)[1]), b'full')
        self.assertEqual(info['items'][0]['quality'], 'cached')
        self.assertEqual(self.generation, original)

    def test_uncertain_native_failure_keeps_verified_thumbnail_without_ui_retry(self):
        with patch('wechat_linux.media.images.read_local_image', return_value=(b'thumb', self.thumb)), \
             patch.object(NativeImageDownloader, 'download', side_effect=ImageDownloadError('native_image_timeout', requested=None)) as download, \
             patch('wechat_linux.desktop.image_download.improve_image') as desktop:
            prompt, info = self.case.prepare(self.generation)
        desktop.assert_not_called()
        download.assert_called_once()
        self.assertEqual(info['items'][0]['quality'], 'thumbnail')
        self.assertEqual(info['items'][0]['download']['backend'], 'native')
        self.assertEqual(info['items'][0]['download']['outcome'], 'native_image_timeout')
        self.assertEqual(base64.b64decode(prompt[1]['content'][-1]['image_url']['url'].split(',', 1)[1]), b'thumb')

    def test_cold_cache_with_verified_source_can_be_fetched_natively(self):
        with patch('wechat_linux.media.images.read_local_image', side_effect=MediaError('local_image_not_cached')), \
             patch.object(NativeImageDownloader, 'download', return_value=Image(b'full', self.full)) as download, \
             patch('wechat_linux.desktop.image_download.improve_image') as desktop:
            prompt, info = self.case.prepare(self.generation)
        download.assert_called_once()
        desktop.assert_not_called()
        self.assertEqual(info['images'], 1)
        self.assertEqual(base64.b64decode(prompt[1]['content'][-1]['image_url']['url'].split(',', 1)[1]), b'full')

    def test_native_mode_without_persisted_intent_never_creates_an_implicit_request(self):
        self.generation['images'][0].pop('native_request')
        with patch('wechat_linux.media.images.read_local_image', return_value=(b'thumb', self.thumb)), \
             patch.object(NativeImageDownloader, 'download') as download, \
             patch('wechat_linux.desktop.image_download.improve_image') as desktop:
            _, info = self.case.prepare(self.generation)
        download.assert_not_called()
        desktop.assert_not_called()
        self.assertEqual(info['items'][0]['quality'], 'thumbnail')

    def test_recovered_native_request_only_queries_helper_status(self):
        client = NativeImageDownloader('/run/synthetic/helper.sock', [native.CHAT], native.ACCOUNT)
        with patch('wechat_linux.native.image_download.read_local_image', side_effect=[native.THUMB, native.FULL]), \
             patch('wechat_linux.native.image_download.request', return_value=deepcopy(native.COMPLETE)) as request:
            result = client.download(native.binding(), chat_id=native.CHAT, request_id='old:request',
                                     timeout=5, resume_only=True)
        self.assertEqual(result.data, native.FULL[0])
        request.assert_called_once()
        self.assertEqual(request.call_args.args[1], {'op':'image_status', 'request_id':'old:request'})

    def test_missing_recovered_id_is_never_resubmitted(self):
        client = NativeImageDownloader('/run/synthetic/helper.sock', [native.CHAT], native.ACCOUNT)
        with patch('wechat_linux.native.image_download.read_local_image', return_value=native.THUMB), \
             patch('wechat_linux.native.image_download.request', return_value=dict(ok=False, error='native_image_request_missing', requested=None)) as request:
            with self.assertRaisesRegex(ImageDownloadError, 'native_image_request_missing'):
                client.download(native.binding(), chat_id=native.CHAT, request_id='old:request',
                                timeout=5, resume_only=True)
        self.assertEqual([c.args[1]['op'] for c in request.call_args_list], ['image_status'])


class NativeImagePipelineTests(unittest.TestCase):
    setUp = pipeline.PipelineTests.setUp
    make_bot = pipeline.PipelineTests.make_bot
    add = pipeline.PipelineTests.add
    restart = pipeline.PipelineTests.restart

    def test_intent_precedes_worker_and_restart_keeps_same_id_and_reader_owner(self):
        self.cfg['queue']['debounce_seconds'] = 0
        self.cfg['vision'].update(download_enabled=True, download_backend='native')
        self.store.image_binding.return_value = dict(account='wxid_self', candidates=[],
            download=dict(verified=True, chat_id='synthetic@chatroom', server_id='123',
                          sender='wxid_friend', image_md5='a'*32))
        item = self.add(1)
        item['reference'] = dict(kind='image', available=False, text='', image_md5='a'*32)
        original_factory = self.bot.job_factory
        def checked_factory(kind, payload, **kwargs):
            if kind == 'generate':
                self.assertTrue(self.bot.journal.head().get('native_image_intents'))
            return original_factory(kind, payload, **kwargs)
        self.bot.job_factory = checked_factory
        self.bot.tick()
        first = next(job for job in self.jobs if job.kind == 'generate')
        request = deepcopy(first.payload[3]['images'][0]['native_request'])
        self.assertFalse(request['resume_only'])
        # A slow image/LLM worker is still pending; the owner keeps receiving.
        self.add(2, mention=False)
        self.bot.tick()
        self.assertEqual(self.bot.last_local_id, 2)
        self.assertIsNone(first.result)
        self.restart()
        self.bot.tick()
        second = next(job for job in reversed(self.jobs) if job.kind == 'generate')
        recovered = second.payload[3]['images'][0]['native_request']
        self.assertEqual(recovered['request_id'], request['request_id'])
        self.assertTrue(recovered['resume_only'])
        self.assertEqual(self.bot.last_local_id, 2)
        # Binding is resolved only by the bot; no store object crosses to worker.
        self.assertIsNot(second.payload[3], self.store)
        self.assertTrue(all(not hasattr(v, 'image_binding') for v in second.payload[3].values()))


if __name__ == '__main__':
    unittest.main()
