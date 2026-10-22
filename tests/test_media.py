"""合成图片、SQLite 附件索引与模拟 API；不读取真实图片或凭据。"""
from __future__ import annotations

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

from wxbridge.config import DEFAULTS, _deep_merge, load_config
from wxbridge.wechat.crypto import AES
from wxbridge.ai.client import DeepSeekClient, DeepSeekError
from wxbridge.ai.prompts import build_prompt
from wxbridge.ai.images import MediaError, V1, V2, decode_dat, key_candidates, prepare_multimodal, read_local_image, validate_image
from wxbridge.wechat.references import image_md5, local_image_cache_key, prompt_reference
from wxbridge.wechat.store import WeChatStore
from synthetic import NOW, msg
from test_references import parse_message, quoted_xml


def png(width=2, height=1):
    def chunk(name, data):
        body = name+data
        return struct.pack(">I", len(data))+body+struct.pack(">I", zlib.crc32(body)&0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\0\xff\0\0\0\0\xff")) + chunk(b"IEND", b""))


def encrypted(data, magic=V2, key=b"synthetic-key-16", xor=77):
    key = b"cfcd208495d565ef" if magic == V1 else key
    prefix, middle, tail = data[:32], data[32:-12], data[-12:]
    padding = 16-len(prefix)%16
    return (magic+struct.pack("<II", len(prefix), len(tail))+b"\0"
            + AES.new(key, AES.MODE_ECB).encrypt(prefix+bytes([padding])*padding)
            + middle+bytes(value^xor for value in tail))


class ImageDecodingTests(unittest.TestCase):
    def test_supported_png_complete_and_dimensions(self):
        self.assertEqual(validate_image(png(), 10000), ("image/png", 2, 1))
        for data in (png()[:-1], png()[:40]+b"corrupt"+png()[47:], png(8193), b"wxgfunsupported"):
            with self.subTest(size=len(data)), self.assertRaises(MediaError):
                validate_image(data, 10000)
        with self.assertRaisesRegex(MediaError, "image_size_limit"):
            validate_image(png(), 10)

    def test_v1_v2_plain_and_legacy_xor_roundtrip(self):
        plain = png()
        self.assertEqual(decode_dat(plain), plain)
        self.assertEqual(decode_dat(bytes(b^93 for b in plain)), plain)
        self.assertEqual(decode_dat(encrypted(plain, V1)), plain)
        self.assertEqual(decode_dat(encrypted(plain), b"synthetic-key-16", 77), plain)

    def test_wrong_key_padding_and_lengths_are_rejected(self):
        data = encrypted(png())
        with self.assertRaisesRegex(MediaError, "image_key_invalid"):
            decode_dat(data, b"wrong-key-123456", 77)
        with self.assertRaisesRegex(MediaError, "image_key_unavailable"):
            decode_dat(data)
        for corrupt in (data[:20], data[:6]+struct.pack("<II", 0xFFFFFFFF, 0xFFFFFFFF)+data[14:]):
            with self.assertRaises(MediaError):
                decode_dat(corrupt, b"synthetic-key-16", 77)


class LocalImageTests(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parents[1]/".test-tmp"
        parent.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.root = self.home/"xwechat_files/wxid_synthetic_suffix"
        self.root.mkdir(parents=True)
        self.path = self.root/"msg/attach/bucket/month/Img/image.dat"
        self.path.parent.mkdir(parents=True)
        self.binding = dict(account="wxid_synthetic", account_root=str(self.root), home=str(self.home),
                            candidates=[dict(relative=str(self.path.relative_to(self.root)), quality="original")])
        self.cfg = _deep_merge(DEFAULTS, {})
        self.cfg["vision"]["cache_wait_seconds"] = 0
        self.cfg["deepseek"].update(system_prompt_file="", system_prompt="合成人格")

    def test_linux_key_derivation_and_v2_in_memory(self):
        folder = self.home/".xwechat/net/kvcomm"
        folder.mkdir(parents=True)
        (folder/"key_123456_test.statistic").touch()
        key = hashlib.md5(b"123456wxid_synthetic").hexdigest()[:16].encode()
        self.assertIn((key, 123456&255), list(key_candidates("wxid_synthetic", self.root, self.home)))
        self.path.write_bytes(encrypted(png(), key=key, xor=123456&255))
        data, info = read_local_image(self.binding, self.cfg)
        self.assertEqual(data, png())
        self.assertEqual(info["quality"], "original")
        self.assertEqual(self.path.read_bytes()[:6], V2)
        self.assertEqual(len(list(self.path.parent.iterdir())), 1)

    def test_unreadable_original_falls_back_to_thumbnail(self):
        self.path.write_bytes(b"wxgfunsupported")
        thumbnail = self.path.with_name("image_t.dat")
        thumbnail.write_bytes(png())
        self.binding["candidates"].append(dict(relative=str(thumbnail.relative_to(self.root)), quality="thumbnail"))
        self.assertEqual(read_local_image(self.binding, self.cfg)[1]["quality"], "thumbnail")

    def test_wxgf_conversion_keeps_pixels_in_memory_and_reports_format(self):
        source = b"wxgf"+bytes(80)
        self.path.write_bytes(encrypted(source))
        with patch('wxbridge.ai.images.key_candidates',return_value=[(b'wrong-key-123456',77),(b'synthetic-key-16',77)]), patch('wxbridge.ai.wxgf.decode_wxgf',return_value=png()) as convert:
            data,info = read_local_image(self.binding,self.cfg)
        self.assertEqual(data,png())
        convert.assert_called_once_with(source,self.cfg['vision']['max_image_bytes'])
        self.assertTrue(info['converted'])
        self.assertEqual(info['source_format'],'wxgf')
        self.assertEqual(len(list(self.path.parent.iterdir())),1)

    def test_wxgf_failure_survives_later_wrong_keys_and_can_fall_back(self):
        from wxbridge.ai.wxgf import WxgfError
        self.path.write_bytes(encrypted(b'wxgf'+bytes(80)))
        with patch('wxbridge.ai.images.key_candidates',return_value=[(b'synthetic-key-16',77),(b'wrong-key-123456',77)]), patch('wxbridge.ai.wxgf.decode_wxgf',side_effect=WxgfError('wxgf_decoder_unavailable')):
            with self.assertRaisesRegex(MediaError,'^wxgf_decoder_unavailable$'):
                read_local_image(self.binding,self.cfg)
            thumbnail = self.path.with_name('image_t.dat')
            thumbnail.write_bytes(png())
            self.binding['candidates'].append(dict(relative=str(thumbnail.relative_to(self.root)),quality='thumbnail'))
            _,info=read_local_image(self.binding,self.cfg)
        self.assertEqual(info['quality'],'thumbnail')
        self.assertEqual(info['fallback_errors'],[dict(quality='original',error='wxgf_decoder_unavailable')])

    def test_missing_oversized_and_escape_paths_fail_closed(self):
        with self.assertRaisesRegex(MediaError, "local_image_not_cached"):
            read_local_image(self.binding, self.cfg)
        self.path.write_bytes(b"0"*1200)
        self.cfg["vision"]["max_image_bytes"] = 100
        with self.assertRaisesRegex(MediaError, "image_size_limit"):
            read_local_image(self.binding, self.cfg)
        self.binding["candidates"][0]["relative"] = "../../outside.dat"
        with self.assertRaisesRegex(MediaError, "unsafe_image_path"):
            read_local_image(self.binding, self.cfg)

    def generation(self, refs=None):
        trigger = msg(10, NOW, "@ds 这怎么看")
        trigger["reference"] = refs or dict(kind="image", available=False, text="", image_md5="a"*32)
        return dict(context=[msg(9, NOW-1, "普通历史")], triggers=[trigger], chat_title="合成群",
                    images=[dict(trigger_index=0, depth=0, **self.binding)])

    def prepare(self, generation):
        base = build_prompt(self.cfg, generation["context"], generation["triggers"][-1], "合成群")
        return prepare_multimodal(self.cfg, base, generation)

    def test_api_receives_actual_pixels_only_in_user_and_keeps_persona(self):
        self.path.write_bytes(png())
        generation = self.generation()
        prompt, metadata = self.prepare(generation)
        self.assertEqual(prompt[0], dict(role="system", content="合成人格"))
        blocks = prompt[1]["content"]
        self.assertEqual(blocks[-1]["type"], "image_url")
        uri = blocks[-1]["image_url"]["url"]
        self.assertEqual(base64.b64decode(uri.split(",", 1)[1]), png())
        self.assertIn('"available": true', blocks[0]["text"])
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
                self.assertIn('[BOT（你）]: 已发出的看图说明', text)
                self.assertNotIn(generation['account'], text)
                self.assertEqual(metadata['images'], int(available))

    def test_missing_pixels_explicitly_unavailable_without_fake_image_block(self):
        prompt, metadata = self.prepare(self.generation())
        self.assertIsInstance(prompt[1]["content"], str)
        self.assertIn('"available": false', prompt[1]["content"])
        self.assertIn("未看到图片", prompt[1]["content"])
        self.assertEqual(metadata["images"], 0)

    def test_nested_image_and_count_limit_with_unambiguous_labels(self):
        self.path.write_bytes(png())
        generation = self.generation(dict(kind="quoted_text", text="中间引用", available=True,
                                         reference=dict(kind="image", available=False, text="")))
        generation["images"][0]["depth"] = 1
        self.cfg["vision"]["max_images"] = 1
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


class ImageBindingTests(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parents[1]/".test-tmp"
        parent.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.chat = "synthetic@chatroom"
        self.bucket = hashlib.md5(self.chat.encode()).hexdigest()
        self.digest, self.filename = "a"*32, "b"*32+".dat"
        self.path = self.root/"hardlink.db"
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.executescript("CREATE TABLE dir2id(username TEXT); CREATE TABLE image_hardlink_info_v4(md5 TEXT,file_name TEXT,dir1 INT,dir2 INT);")
            conn.executemany("INSERT INTO dir2id VALUES (?)", [(self.bucket,), ("2026-10",)])
            conn.execute("INSERT INTO image_hardlink_info_v4 VALUES (?,?,1,2)", (self.digest, self.filename))
        self.store = WeChatStore.__new__(WeChatStore)
        self.store.db_dir = str(self.root/"db_storage")
        self.store._account_username = "wxid_synthetic"
        self.store._query_messages = Mock(return_value=[])
        def connect(rel):
            self.assertEqual(rel, "hardlink/hardlink.db")
            conn = sqlite3.connect(self.path)
            conn.row_factory = sqlite3.Row
            return conn
        self.store._connect = connect

    def test_md5_resolves_opaque_file_name_not_md5_guess(self):
        result = self.store.image_binding(self.chat, dict(image_md5=self.digest))
        self.assertNotIn("error", result)
        self.assertIn("b"*32+".dat", result["candidates"][1]["relative"])
        self.assertEqual([c["quality"] for c in result["candidates"]], ["original", "cached", "preview", "thumbnail"])

    def test_original_packed_cache_key_resolves_when_xml_md5_is_not_indexed(self):
        self.store._query_messages.return_value = [dict(type=3, image_md5="c"*32, sender="friend", image_cache_key="b"*32)]
        binding = self.store.image_binding(self.chat, dict(server_id="123", sender="friend"))
        self.assertNotIn("error", binding)
        self.assertIn(self.filename, binding["candidates"][1]["relative"])

    def test_unproven_or_forged_cache_key_cannot_select_attachment(self):
        ref = dict(image_md5="c"*32, image_cache_key="b"*32)
        self.assertEqual(self.store.image_binding(self.chat, ref)["error"], "local_image_index_missing")
        self.store._query_messages.return_value = [dict(type=3, image_md5="c"*32, sender="friend", image_cache_key="../unsafe")]
        self.assertEqual(self.store.image_binding(self.chat, dict(server_id="123"))["error"], "local_image_index_missing")

    def test_local_cache_key_requires_exact_complete_protobuf_path(self):
        token = b"b"*32
        nested = b"\x22\x20"+token
        packed = b"\x08\x01\x10\x02\x1a\x22"+nested+b"\x58\x01"
        self.assertEqual(local_image_cache_key(packed), token.decode())
        for invalid in (None, token, b"\x1a\x23"+nested, b"\x1a\x22"+nested+b"\x80", packed+packed,
                        b"\x1a\x44"+nested+nested, b"\x12\x22"+nested, b"\x1a\x22\x2a\x20"+token):
            with self.subTest(invalid_type=type(invalid).__name__):
                self.assertEqual(local_image_cache_key(invalid), "")

    def test_server_id_recovers_missing_snapshot_md5_only_in_current_chat(self):
        self.store._query_messages.return_value = [dict(type=3, image_md5=self.digest, sender="friend")]
        result = self.store.image_binding(self.chat, dict(server_id="123", sender="friend"))
        self.store._query_messages.assert_called_once_with(self.chat, 2, server_id="123")
        self.assertTrue(result["candidates"])
        result = self.store.image_binding(self.chat, dict(server_id="123", image_md5="c"*32))
        self.assertEqual(result["error"], "image_reference_mismatch")

    def test_other_chat_bucket_requires_matching_original_in_current_chat(self):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("UPDATE dir2id SET username=? WHERE rowid=1", ("d"*32,))
        self.assertEqual(self.store.image_binding(self.chat, dict(image_md5=self.digest))["error"], "local_image_index_missing")
        self.store._query_messages.return_value = [dict(type=3, image_md5=self.digest, sender="friend")]
        self.assertTrue(self.store.image_binding(self.chat, dict(server_id="123", image_md5=self.digest))["candidates"])

    def test_untrusted_index_path_rejected(self):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("UPDATE image_hardlink_info_v4 SET file_name='../../secret.dat'")
        self.assertFalse(self.store.image_binding(self.chat, dict(image_md5=self.digest))["candidates"])

    def test_xml_strips_sensitive_download_fields(self):
        xml = '<msg><img md5="'+self.digest+'" aeskey="PRIVATE_KEY" cdnmidimgurl="PRIVATE_URL"/></msg>'
        parsed = parse_message(quoted_xml(content=xml, kind=3))
        self.assertEqual(parsed["reference"]["image_md5"], self.digest)
        self.assertNotIn("PRIVATE", json.dumps(parsed))
        self.assertNotIn(self.digest, json.dumps(prompt_reference(parsed["reference"])))
        self.assertEqual(image_md5("<!DOCTYPE msg><msg/>"), "")
        self.assertEqual(image_md5('<msg><img md5="../unsafe"/></msg>'), "")

    def test_real_group_quote_fromusr_is_chat_chatusr_is_author(self):
        xml = quoted_xml(kind=3, content='<msg><img md5="'+self.digest+'"/></msg>', sender=self.chat)
        xml = xml.replace('<chatusr>synthetic@chatroom</chatusr>', '<chatusr>friend</chatusr>')
        reference = parse_message(xml)["reference"]
        self.assertEqual(reference["sender"], "friend")
        self.assertEqual(reference["chat_id"], self.chat)
        self.store._query_messages.return_value = [dict(type=3, image_md5=self.digest, sender="friend")]
        self.assertTrue(self.store.image_binding(self.chat, reference)["candidates"])
        reference["sender"] = "different_author"
        self.assertEqual(self.store.image_binding(self.chat, reference)["error"], "image_reference_mismatch")

    def test_other_chat_reference_rejected_even_with_identical_md5(self):
        result = self.store.image_binding(self.chat, dict(chat_id="other@chatroom", image_md5=self.digest))
        self.assertEqual(result["error"], "image_reference_wrong_chat")
        self.store._query_messages.assert_not_called()


class VisionClientTests(unittest.TestCase):
    def test_post_sends_openai_image_content_array_and_rejects_request_limit(self):
        client = DeepSeekClient.__new__(DeepSeekClient)
        client.base_url, client.api_key = "https://example.invalid", "synthetic-key"
        payload = dict(messages=[dict(role="user", content=[dict(type="image_url", image_url=dict(url="data:image/png;base64,AAAA"))])])
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"choices": []}'
        with patch("wxbridge.ai.client.urllib.request.urlopen", return_value=response) as send:
            client._post(payload, 1)
            self.assertEqual(json.loads(send.call_args.args[0].data), payload)
        with patch("wxbridge.ai.client.MAX_REQUEST_BYTES", 10), patch("wxbridge.ai.client.urllib.request.urlopen") as send:
            with self.assertRaises(DeepSeekError):
                client._post(payload, 1)
            send.assert_not_called()

    def test_default_vision_and_invalid_config(self):
        with patch("wxbridge.config.load_dotenv"):
            cfg = load_config(Path(__file__).parent/"nonexistent.json")
        self.assertTrue(cfg["vision"]["enabled"])
        self.assertEqual(cfg["queue"]["debounce_seconds"], 0)
