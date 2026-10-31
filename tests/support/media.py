"""Shared synthetic media fixtures; contains no executable tests."""
from __future__ import annotations
from tests.support.model_support import default_config
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest
from unittest.mock import Mock
import zlib
from wechat_linux.storage.crypto import AES
from wxbridge.ai.prompts import build_prompt
from wxbridge.ai.images import V1, V2, prepare_multimodal
from wxbridge.wechat.store import WeChatStore
from tests.support.synthetic import NOW, msg
from tests.support.paths import ROOT


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


class LocalImageFixture(unittest.TestCase):
    def setUp(self):
        parent = ROOT/".test-tmp"
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
        self.cfg = default_config()
        self.cfg["vision"]["cache_wait_seconds"] = 0

        self.cfg['system_prompt_file'] = str(ROOT/'prompts/system.md')

    def generation(self, refs=None):
        trigger = msg(10, NOW, "@ds 这怎么看")
        trigger["reference"] = refs or dict(kind="image", available=False, text="", image_md5="a"*32)
        return dict(context=[msg(9, NOW-1, "普通历史")], triggers=[trigger], chat_title="合成群",
                    images=[dict(trigger_index=0, depth=0, **self.binding)])

    def prepare(self, generation):
        base = build_prompt(self.cfg, generation["context"], generation["triggers"][-1], "合成群",
                            generation['triggers'], memory=generation.get('memory'), account=generation.get('account', ''))
        return prepare_multimodal(self.cfg, base, generation)


class ImageBindingFixture(unittest.TestCase):
    def setUp(self):
        parent = ROOT/".test-tmp"
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

    def indexless_source(self, *, month="2026-10", bucket=None, suffix="_t"):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("DELETE FROM image_hardlink_info_v4")
        self.store._query_messages.return_value = [dict(type=3, image_md5=self.digest,
                                                        sender="friend", image_cache_key="b"*32)]
        picture = self.root/"msg/attach"/(bucket or self.bucket)/month/"Img"/("b"*32+suffix+".dat")
        picture.parent.mkdir(parents=True, exist_ok=True)
        picture.write_bytes(png())
        return picture, dict(kind="image", server_id="123", sender="friend", chat_id=self.chat,
                             image_md5=self.digest, available=False, text="")
