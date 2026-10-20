"""验证 SQLCipher 页布局、HMAC 和 WAL patch。

用随机页做纯算法测试，不依赖真实微信数据；可在 Windows/Linux 运行。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import struct
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from Crypto.Cipher import AES
except ImportError:
    from Cryptodome.Cipher import AES

from wxbridge.wechat import crypto


def make_plain_page(pgno: int) -> bytes:
    if pgno == 1:
        body = crypto.SQLITE_HDR + os.urandom(crypto.PAGE_SZ - 16 - crypto.RESERVE_SZ)
    else:
        body = os.urandom(crypto.PAGE_SZ - crypto.RESERVE_SZ)
    return body + b"\x00" * crypto.RESERVE_SZ


def encrypt_page(plain: bytes, pgno: int, enc_key: bytes, salt: bytes) -> bytes:
    iv = os.urandom(16)
    cipher = AES.new(enc_key, AES.MODE_CBC, iv)
    if pgno == 1:
        ct = cipher.encrypt(plain[16 : crypto.PAGE_SZ - crypto.RESERVE_SZ])
        prefix = salt
        mac_data = ct + iv
        enc = salt + ct + iv
    else:
        ct = cipher.encrypt(plain[: crypto.PAGE_SZ - crypto.RESERVE_SZ])
        mac_data = ct + iv
        enc = ct + iv
    mac_key = crypto.derive_mac_key(enc_key, salt)
    mac = hmac.new(mac_key, mac_data, hashlib.sha512)
    mac.update(struct.pack("<I", pgno))
    return enc + mac.digest()


def main() -> None:
    key = os.urandom(32)
    salt = os.urandom(16)

    # 1) page 1 / page N 往返
    for pgno in (1, 2, 7):
        plain = make_plain_page(pgno)
        enc = encrypt_page(plain, pgno, key, salt)
        assert len(enc) == crypto.PAGE_SZ
        if pgno == 1:
            assert crypto.verify_page1(key, enc), "HMAC 应通过"
            assert not crypto.verify_page1(os.urandom(32), enc), "错误 key 不应通过"
        out = crypto.decrypt_page(key, enc, pgno)
        assert out == plain, f"page {pgno} 往返不一致"
    print("OK: page 1/N round-trip + HMAC")

    # 2) WAL patch
    with tempfile.TemporaryDirectory() as td:
        plain_path = os.path.join(td, "plain.db")
        wal_path = plain_path + "-wal"
        # 明文库：2 页
        p1 = make_plain_page(1)
        p2 = make_plain_page(2)
        with open(plain_path, "wb") as f:
            f.write(p1 + p2)
        # WAL：header + 1 帧（page 2 的新内容）
        new_p2 = make_plain_page(2)
        enc_p2 = encrypt_page(new_p2, 2, key, salt)
        salt1, salt2 = struct.unpack(">II", salt[:8])
        with open(wal_path, "wb") as f:
            hdr = bytearray(32)
            hdr[0:4] = struct.pack(">I", 0x377F0682)
            hdr[4:8] = struct.pack(">I", 3007000)
            hdr[8:12] = struct.pack(">I", crypto.PAGE_SZ)
            hdr[16:20] = struct.pack(">I", salt1)
            hdr[20:24] = struct.pack(">I", salt2)
            f.write(hdr)
            fh = bytearray(24)
            fh[0:4] = struct.pack(">I", 2)  # pgno
            fh[4:8] = struct.pack('>I', 2)  # 提交后数据库大小
            fh[8:12] = struct.pack(">I", salt1)
            fh[12:16] = struct.pack(">I", salt2)
            f.write(fh)
            f.write(enc_p2)
        n = crypto.apply_wal(wal_path, plain_path, key, salt)
        assert n == 1, n
        with open(plain_path, "rb") as f:
            data = f.read()
        assert data[4096:8192] == new_p2, "WAL patch 后 page 2 不一致"
        print("OK: WAL patch")

        # 增长到第 3 页，然后追加未提交的第 2 页修改；未提交内容不能进入副本。
        new_p3 = make_plain_page(3)
        uncommitted_p2 = make_plain_page(2)
        with open(wal_path, 'ab') as f:
            for pgno, commit_size, plain in [(3, 3, new_p3), (2, 0, uncommitted_p2)]:
                frame = bytearray(24)
                frame[:8] = struct.pack('>II', pgno, commit_size)
                frame[8:16] = struct.pack('>II', salt1, salt2)
                f.write(frame)
                f.write(encrypt_page(plain, pgno, key, salt))
        n = crypto.apply_wal(wal_path, plain_path, key, salt)
        with open(plain_path, 'rb') as f:
            committed_data = f.read()
        assert n == 2 and len(committed_data) == 3 * crypto.PAGE_SZ
        assert committed_data[4096:8192] == new_p2
        assert committed_data[8192:] == new_p3
        print('OK: WAL growth + uncommitted tail excluded')

        # 已提交密文损坏时，整次回放必须拒绝，不能部分覆盖副本。
        with open(wal_path, 'r+b') as f:
            f.seek(32 + 24 + 100)
            old_byte = f.read(1)
            f.seek(-1, 1)
            f.write(bytes([old_byte[0] ^ 1]))
        try:
            crypto.apply_wal(wal_path, plain_path, key, salt)
        except ValueError:
            pass
        else:
            raise AssertionError('损坏的 WAL 应被拒绝')
        with open(plain_path, 'rb') as f:
            assert f.read() == committed_data
        print('OK: WAL corruption rejected before modifying snapshot')


if __name__ == "__main__":
    main()
