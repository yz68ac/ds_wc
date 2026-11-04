"""合成图片、SQLite 附件索引与模拟 API；不读取真实图片或凭据。"""
from __future__ import annotations
from tests.support.configuration import replace_config, update_config, snapshot
from tests.support.model_support import default_config
from contextlib import nullcontext

import base64
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest
from unittest.mock import Mock, patch
import zlib

from wxbridge.config import load_config
from wxbridge.config import default_document
from tests.support.configuration import merge_config as _deep_merge
from wechat_linux.storage.crypto import AES
from tests.support.model_support import ClientFixture, ModelError
from wxbridge.ai.reply.prompt import build_prompt
from wxbridge.ai.reply.images import prepare_multimodal
from wechat_linux.media import MediaError, V1, V2, decode_dat, key_candidates, read_local_image, validate_image
from wechat_linux.messages.references import image_md5, local_image_cache_key
from wxbridge.ai.context.identity import prompt_reference
from wxbridge.wechat.store import WeChatStore
from tests.support.synthetic import NOW, msg
from tests.support.messages import parse_message, quoted_xml
from tests.support.media import png, encrypted, LocalImageFixture, ImageBindingFixture




class LocalImageTests(LocalImageFixture):







    def test_api_receives_actual_pixels_only_in_user_and_keeps_persona(self):
        self.path.write_bytes(png())
        generation = self.generation()
        prompt, metadata = self.prepare(generation)
        from wxbridge.ai.reply.prompt import system_prompt
        self.assertEqual(prompt[0], dict(role='system', content=system_prompt(self.cfg)))
        blocks = prompt[1]["content"]
        self.assertEqual(blocks[-1]["type"], "image_url")
        uri = blocks[-1]["image_url"]["url"]
        self.assertEqual(base64.b64decode(uri.split(",", 1)[1]), png())
        self.assertIn('"available":true', blocks[0]["text"])
        self.assertIn("普通历史", blocks[0]["text"])
        self.assertNotIn("image_md5", blocks[0]["text"])
        self.assertEqual(metadata["images"], 1)
        self.assertNotIn("base64", json.dumps(metadata))
        self.assertNotIn("account_root", json.dumps(metadata))
        self.assertFalse(generation["triggers"][0]["reference"]["available"])

    def test_image_rebuild_keeps_own_identity_with_and_without_actual_pixels(self):
        for available in (False, True):
            with self.subTest(available=available):
                if available:
                    self.path.write_bytes(png())
                generation = self.generation()
                generation['account'] = 'wxid_synthetic_bot_account'
                generation['context'].append(msg(8, NOW-1, '已发出的看图说明', generation['account']))
                prompt, metadata = self.prepare(generation)
                body = prompt[1]['content']
                text = body[0]['text'] if isinstance(body, list) else body
                data = json.loads(text)
                self.assertEqual(data['bot'], generation['account'])
                self.assertEqual(data['messages'][0]['sender'], generation['account'])
                self.assertFalse(any('is_bot' in m for m in data['messages']))
                self.assertEqual(metadata['images'], int(available))

    def test_missing_pixels_explicitly_unavailable_without_fake_image_block(self):
        prompt, metadata = self.prepare(self.generation())
        self.assertIsInstance(prompt[1]["content"], str)
        self.assertIn('"available":false', prompt[1]["content"])
        self.assertIn("未看到图片", prompt[1]["content"])
        self.assertEqual(metadata["images"], 0)

    def test_nested_image_and_count_limit_with_unambiguous_labels(self):
        self.path.write_bytes(png())
        generation = self.generation(dict(kind="quoted_text", text="中间引用", available=True,
                                         reference=dict(kind="image", available=False, text="")))
        generation["images"][0]["depth"] = 1
        self.cfg = replace_config(self.cfg, 'vision.max_images', 1, owner=self)
        # 同一图的另一个明确引用；上限后不传第二份图片。
        import copy
        generation["triggers"].append(copy.deepcopy(generation["triggers"][0]))
        generation["triggers"][1]["local_id"] = 11
        generation["images"].append(dict(generation["images"][0], trigger_index=1))
        prompt, metadata = self.prepare(generation)
        self.assertEqual(metadata["images"], 1)
        self.assertEqual(metadata["items"][1]["error"], "image_count_limit")
        self.assertEqual(sum(b["type"] == "image_url" for b in prompt[1]["content"]), 1)
        self.assertIn("引用图片1", prompt[1]["content"][-2]["text"])


class ImageBindingTests(ImageBindingFixture):




    def test_indexless_thumbnail_reaches_multimodal_request_with_quality_label(self):
        picture, reference = self.indexless_source()
        binding = self.store.image_binding(self.chat, reference)
        self.assertNotIn("error", binding)
        self.assertTrue(binding["cache_fallback"])
        trigger = msg(10, NOW, "@ds 这怎么看")
        trigger["reference"] = reference
        cfg = default_config()

        cfg = replace_config(cfg, 'system_prompt_file', str(Path(__file__).resolve().parents[1]/'prompts/system.md'))
        generation = dict(context=[], triggers=[trigger], chat_title="合成群",
                          images=[dict(trigger_index=0, depth=0, **binding)])
        base = build_prompt(cfg, [], trigger, "合成群")
        prompt, meta = prepare_multimodal(cfg, base, generation)
        self.assertEqual(meta["images"], 1)
        self.assertEqual(meta["items"][0]["quality"], "thumbnail")
        self.assertEqual(meta["items"][0]["binding_source"], "verified_message_cache")
        self.assertIn("本地缩略图", prompt[1]["content"][-2]["text"])
        self.assertEqual(base64.b64decode(prompt[1]["content"][-1]["image_url"]["url"].split(",", 1)[1]), png())
        self.assertEqual(picture.read_bytes(), png())
        self.assertEqual(len(list(picture.parent.iterdir())), 1)
        self.assertNotIn("relative", json.dumps(meta))






    def test_indexless_cache_wait_observes_later_full_image_without_database_refresh(self):
        thumbnail, reference = self.indexless_source()
        thumbnail.unlink()
        binding = self.store.image_binding(self.chat, reference)
        full = thumbnail.with_name("b"*32+"_h.dat")
        trigger = msg(10, NOW, "@ds 这怎么看")
        trigger["reference"] = reference
        cfg = default_config()

        cfg = replace_config(cfg, 'system_prompt_file', str(Path(__file__).resolve().parents[1]/'prompts/system.md'))
        generation = dict(context=[], triggers=[trigger], chat_title="合成群",
                          images=[dict(trigger_index=0, depth=0, **binding)])
        self.store._query_messages.reset_mock()
        with patch("wxbridge.ai.reply.images.time.sleep", side_effect=lambda _: full.write_bytes(png())):
            _, meta = prepare_multimodal(cfg, build_prompt(cfg, [], trigger, "合成群"), generation)
        self.assertEqual(meta["images"], 1)
        self.assertEqual(meta["items"][0]["quality"], "original")
        self.store._query_messages.assert_not_called()







    def test_xml_strips_sensitive_download_fields(self):
        xml = '<msg><img md5="'+self.digest+'" aeskey="PRIVATE_KEY" cdnmidimgurl="PRIVATE_URL"/></msg>'
        parsed = parse_message(quoted_xml(content=xml, kind=3))
        self.assertEqual(parsed["reference"]["image_md5"], self.digest)
        self.assertNotIn("PRIVATE", json.dumps(parsed))
        self.assertNotIn(self.digest, json.dumps(prompt_reference(parsed["reference"])))
        self.assertEqual(image_md5("<!DOCTYPE msg><msg/>"), "")
        self.assertEqual(image_md5('<msg><img md5="../unsafe"/></msg>'), "")




class VisionClientTests(unittest.TestCase):
    def test_post_sends_openai_image_content_array_and_rejects_request_limit(self):
        client = ClientFixture.__new__(ClientFixture)
        from provider.adapters import get_protocol_adapter
        client.adapter = get_protocol_adapter('deepseek')
        client.cfg = {}
        client.protocol = 'deepseek'
        client._sdk, client._sdk_client = Mock(), Mock()
        client.base_url, client.api_key = "https://example.invalid", "synthetic-key"
        payload = dict(messages=[dict(role="user", content=[dict(type="image_url", image_url=dict(url="data:image/png;base64,AAAA"))])])
        with patch("provider.transport.sdk_send", return_value={"choices": []}) as send:
            client._post(payload, 1)
            self.assertEqual(send.call_args.args[3], payload)
            self.assertEqual(send.call_args.args[2], "deepseek")
        with patch("provider.transport.MAX_REQUEST_BYTES", 10), patch("provider.transport.sdk_send") as send:
            with self.assertRaises(ModelError):
                client._post(payload, 1)
            send.assert_not_called()

    def test_default_vision_and_invalid_config(self):
        with nullcontext():
            cfg = snapshot(default_document())
        self.assertTrue(cfg.vision.enabled)
        self.assertEqual(cfg.participation.debounce_seconds, 2)
        self.assertFalse(hasattr(cfg.queue,'debounce_seconds'))
        self.assertFalse(hasattr(cfg.target,'trigger'))
