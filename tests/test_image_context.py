"""Synthetic history pictures, source provenance and confirmed-artifact retention."""
import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from tests.support import media, pipeline
from tests.support.configuration import replace_config
from tests.support.synthetic import NOW, msg
from wxbridge.ai.context.images import image_candidates
from wxbridge.ai.context.selection import ContextError, select_memory_context
from wxbridge.ai.composer import PromptComposer
from wxbridge.ai.reply.images import prepare_multimodal
from wxbridge.ai.reply.prompt import build_prompt
from wxbridge.storage.image_artifacts import artifact_directory


class HistoryImageTests(unittest.TestCase):
    setUp = media.LocalImageFixture.setUp

    def setup_history(self, count=1):
        self.cfg = replace_config(self.cfg, 'vision.context_images_enabled', True)
        self.path.write_bytes(media.png())
        pictures = [dict(msg(i, NOW-10+i, ''), type=3, image_md5=f'{i:032x}', server_id=str(100+i))
                    for i in range(1, count+1)]
        return dict(context=pictures, triggers=[msg(10, NOW, '这张图是什么？')], chat_title='合成群')

    def base(self, generation):
        return build_prompt(self.cfg, generation['context'], generation['triggers'][-1], generation['chat_title'],
                            generation['triggers'], account=generation.get('account', ''))

    def prepare(self, generation):
        base = self.base(generation)
        generation['images'] = [dict(self.binding, targets=c['targets'])
                                for c in image_candidates(self.cfg, generation['context'], generation['triggers'], base)]
        return prepare_multimodal(self.cfg, base, generation)

    def test_disabled_defaults_keep_history_text_only(self):
        generation = self.setup_history()
        self.cfg = replace_config(self.cfg, 'vision.context_images_enabled', False)
        base = self.base(generation)
        self.assertEqual([m['id'] for m in json.loads(base[1]['content'])['messages']], [10])
        self.assertEqual(image_candidates(self.cfg, generation['context'], generation['triggers'], base), [])

    def test_history_picture_reaches_model_without_quote_and_keeps_author_time(self):
        generation = self.setup_history()
        original = deepcopy(generation)
        prompt, info = self.prepare(generation)
        payload = json.loads(prompt[1]['content'][0]['text'])
        picture = payload['messages'][0]
        self.assertEqual((picture['id'], picture['sender'], picture['time']), (1, 'wxid_friend', NOW-9))
        self.assertTrue(picture['media']['available'])
        self.assertEqual(info['images'], 1)
        self.assertEqual(base64.b64decode(prompt[1]['content'][-1]['image_url']['url'].split(',')[1]), media.png())
        self.assertNotIn('image_md5', json.dumps(payload))
        self.assertNotIn('account_root', json.dumps(info))
        self.assertEqual(generation['context'], original['context'])

    def test_quotes_take_priority_and_duplicate_history_uses_one_block(self):
        generation = self.setup_history(2)
        self.cfg = replace_config(self.cfg, 'vision.max_images', 1)
        picture = generation['context'][0]
        generation['triggers'][0]['reference'] = dict(kind='image', sender=picture['sender'],
            server_id=picture['server_id'], image_md5=picture['image_md5'], available=False, text='')
        prompt, info = self.prepare(generation)
        payload = json.loads(prompt[1]['content'][0]['text'])
        self.assertEqual(info['images'], 1)
        self.assertEqual(payload['messages'][0]['media']['image_label'], payload['messages'][-1]['reference']['image_label'])
        self.assertFalse(payload['messages'][1]['media']['available'])

    def test_recent_pictures_win_shared_limit(self):
        generation = self.setup_history(3)
        self.cfg = replace_config(self.cfg, 'vision.max_images', 1)
        prompt, info = self.prepare(generation)
        rows = json.loads(prompt[1]['content'][0]['text'])['messages']
        self.assertEqual([row['id'] for row in rows if row.get('media', {}).get('available')], [3])
        self.assertEqual(info['images'], 1)

    def test_digestless_quote_deduplicates_by_server_id_but_conflicts_do_not(self):
        generation = self.setup_history()
        picture = generation['context'][0]
        generation['triggers'][0]['reference'] = dict(kind='image', sender=picture['sender'],
            server_id=picture['server_id'], available=False, text='')
        candidates = image_candidates(self.cfg, generation['context'], generation['triggers'], self.base(generation))
        self.assertEqual(len(candidates), 1)
        self.assertEqual(len(candidates[0]['targets']), 2)
        self.assertEqual(candidates[0]['source']['local_id'], 1)
        generation['triggers'][0]['reference']['image_md5'] = 'f'*32
        candidates = image_candidates(self.cfg, generation['context'], generation['triggers'], self.base(generation))
        self.assertEqual(len(candidates), 2)

    def test_missing_picture_keeps_text_request_and_explicit_unavailability(self):
        generation = self.setup_history()
        self.path.unlink()
        prompt, info = self.prepare(generation)
        self.assertIsInstance(prompt[1]['content'], str)
        payload = json.loads(prompt[1]['content'])
        self.assertFalse(payload['messages'][0]['media']['available'])
        self.assertEqual(payload['messages'][-1]['text'], '这张图是什么？')
        self.assertEqual(info['images'], 0)

    def test_budget_removal_does_not_leave_an_orphan_image_block(self):
        generation = self.setup_history()
        length = len(self.base(generation)[1]['content'])
        self.cfg = replace_config(self.cfg, 'target.context_max_chars', length+1)
        prompt, info = self.prepare(generation)
        self.assertIsInstance(prompt[1]['content'], str)
        payload = json.loads(prompt[1]['content'])
        self.assertEqual([m['id'] for m in payload['messages']], [10])
        self.assertEqual(info['images'], 0)
        self.assertEqual(info['items'][0]['error'], 'image_context_omitted')

    def test_source_tampering_and_other_account_are_rejected(self):
        generation = self.setup_history()
        candidate = image_candidates(self.cfg, generation['context'], generation['triggers'], self.base(generation))[0]
        target = candidate['targets'][0]
        update = dict(target, source=(999, *target['source'][1:]), media=dict(available=True))
        with self.assertRaises(ContextError):
            PromptComposer.references(self.base(generation), generation['triggers'], account='', media=[update])
        with self.assertRaises(ContextError):
            PromptComposer.references(self.base(generation), generation['triggers'], account='wrong', media=[])

    def test_memory_and_participation_remain_text_only(self):
        from wxbridge.ai.context.chat import ChatContext
        generation = self.setup_history()
        self.assertEqual(select_memory_context(self.cfg, generation['context'], generation['triggers'][0]), [])
        chat = ChatContext.build(self.cfg, generation['context'], generation['triggers'], '合成群')
        self.assertEqual([m['id'] for m in chat.payload['messages']], [10])

    def test_wrong_chat_quote_cannot_borrow_history_binding(self):
        generation = self.setup_history()
        picture = generation['context'][0]
        generation['triggers'][0]['reference'] = dict(kind='image', sender=picture['sender'],
            image_md5=picture['image_md5'], chat_id='other@chatroom', available=False, text='')
        candidates = image_candidates(self.cfg, generation['context'], generation['triggers'], self.base(generation))
        self.assertEqual(len(candidates), 2)

    def test_conflicting_quote_server_id_cannot_borrow_history_proof(self):
        generation = self.setup_history()
        picture = generation['context'][0]
        generation['triggers'][0]['reference'] = dict(kind='image', sender=picture['sender'],
            image_md5=picture['image_md5'], server_id='999', available=False, text='')
        candidates = image_candidates(self.cfg, generation['context'], generation['triggers'], self.base(generation))
        self.assertEqual(len(candidates), 2)

    def test_prepared_prompt_cannot_be_bound_twice(self):
        generation = self.setup_history()
        prompt, _ = self.prepare(generation)
        with self.assertRaises(ContextError):
            prepare_multimodal(self.cfg, prompt, generation)

    def test_generated_artifact_is_read_without_wechat_cache_or_download(self):
        generation = self.setup_history()
        self.cfg = replace_config(self.cfg, 'paths.queue_file', str(self.home/'queue.sqlite3'))
        generation['account'] = self.binding['account']
        data = media.png()
        filename = 'a'*32+'.png'
        artifact = artifact_directory(self.cfg.paths.queue_file)/filename
        artifact.write_bytes(data)
        artifact.chmod(0o600)
        image = dict(file=filename, bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
        base = self.base(generation)
        candidate = image_candidates(self.cfg, generation['context'], generation['triggers'], base)[0]
        generation['images'] = [dict(account=generation['account'], generated_image=image, targets=candidate['targets'])]
        with patch('wechat_linux.media.images.read_local_image', side_effect=AssertionError('cache not needed')):
            prompt, info = prepare_multimodal(self.cfg, base, generation)
        self.assertEqual(info['images'], 1)
        artifact.write_bytes(b'changed')
        _, info = prepare_multimodal(self.cfg, base, generation)
        self.assertEqual(info['images'], 0)
        self.assertEqual(info['items'][0]['error'], 'image_artifact_changed')

    def test_context_native_download_uses_existing_source_intent(self):
        from wechat_linux import NativeImageDownloader
        from wechat_linux.models import Image
        generation = self.setup_history()
        generation['account'] = self.binding['account']
        self.cfg = replace_config(self.cfg, 'vision.download_enabled', True)
        self.cfg = replace_config(self.cfg, 'vision.download_backend', 'native')
        self.cfg = replace_config(self.cfg, 'account.helper_transport.allowed_chat_ids', ('synthetic@chatroom',))
        base = self.base(generation)
        candidate = image_candidates(self.cfg, generation['context'], generation['triggers'], base)[0]
        generation['images'] = [dict(self.binding, targets=candidate['targets'],
            download=dict(verified=True, chat_id='synthetic@chatroom'),
            native_request=dict(request_id='bot-image:synthetic', resume_only=False))]
        info = dict(mime='image/png', width=2, height=1, bytes=len(media.png()), quality='cached')
        with patch.object(NativeImageDownloader, 'download', return_value=Image(media.png(), info)) as download:
            _, result = prepare_multimodal(self.cfg, base, generation)
        self.assertEqual(result['images'], 1)
        self.assertEqual(download.call_args.kwargs['request_id'], 'bot-image:synthetic')


class HistoryPipelineTests(unittest.TestCase):
    setUp = pipeline.PipelineFixture.setUp
    make_bot = pipeline.PipelineFixture.make_bot
    add = pipeline.PipelineFixture.add
    restart = pipeline.PipelineFixture.restart

    def test_unquoted_history_binding_is_owner_only_and_download_intent_is_durable(self):
        self.cfg = replace_config(self.cfg, 'vision.context_images_enabled', True, owner=self)
        self.cfg = replace_config(self.cfg, 'vision.download_enabled', True, owner=self)
        self.cfg = replace_config(self.cfg, 'vision.download_backend', 'native', owner=self)
        picture = dict(self.add(1, mention=False), type=3, text='', image_md5='a'*32, server_id='123')
        self.rows[0] = picture
        self.add(2)
        self.store.image_binding.return_value = dict(account='wxid_self', candidates=[],
            download=dict(verified=True, chat_id=self.bot.chat_id, sender='wxid_friend', server_id='123', image_md5='a'*32))
        self.bot.tick()
        job = next(j for j in self.jobs if j.kind == 'generate')
        self.store.image_binding.assert_called_once_with(self.bot.chat_id, picture)
        binding = job.payload[3]['images'][0]
        self.assertEqual(binding['targets'][0]['message_id'], 1)
        request = binding['native_request']
        self.assertFalse(request['resume_only'])
        self.assertNotIn('base64', json.dumps(self.bot.journal.head()))
        self.restart()
        self.bot.tick()
        resumed = next(j for j in reversed(self.jobs) if j.kind == 'generate').payload[3]['images'][0]['native_request']
        self.assertEqual(resumed['request_id'], request['request_id'])
        self.assertTrue(resumed['resume_only'])

    def test_index_is_identity_bound_bounded_and_survives_restart(self):
        journal = self.bot.journal
        for i in range(20):
            with journal.db:
                journal.remember_image(i+1, dict(file=f'{i:032x}.png', md5='a'*32, bytes=8*1024*1024), self.now, 60)
        self.assertEqual(len(journal.context_images(self.now)), 8)
        source = dict(local_id=20, sender='wxid_self', image_md5='a'*32)
        self.assertIsNotNone(journal.context_image(source, self.now))
        self.assertIsNone(journal.context_image(dict(source, sender='other'), self.now))
        self.assertIsNone(journal.context_image(dict(source, image_md5='b'*32), self.now))
        self.assertIsNone(journal.context_image(source, self.now+61))
        self.cfg = replace_config(self.cfg, 'vision.context_images_enabled', True, owner=self)
        self.restart()
        self.assertIsNotNone(self.bot.journal.context_image(source, self.now))


class DirectImageBindingTests(unittest.TestCase):
    setUp = media.ImageBindingFixture.setUp

    def test_direct_message_is_verified_by_local_id_even_without_server_id(self):
        source = dict(local_id=5, type=3, time=NOW, image_md5=self.digest, sender='friend')
        self.store._query_messages.return_value = [dict(source, image_cache_key='b'*32)]
        value = self.store.image_binding(self.chat, source)
        self.store._query_messages.assert_called_once_with(self.chat, 2, local_id=5)
        self.assertNotIn('error', value)
        self.assertNotIn('download', value)
        self.store._query_messages.return_value = [dict(source, time=NOW+1)]
        self.assertEqual(self.store.image_binding(self.chat, source)['error'], 'image_reference_mismatch')
        self.store._query_messages.return_value = []
        self.assertEqual(self.store.image_binding(self.chat, source)['error'], 'image_reference_mismatch')
