"""SQLCipher 4 数据库解密。

微信 4.x 使用 WCDB（SQLCipher 4）：
  AES-256-CBC, page=4096, reserve=80 (IV 16 + HMAC 64)
  KDF: PBKDF2-HMAC-SHA512, 迭代 256000（这里拿到的已经是派生后的 raw key）

加密页布局：
  page 1:  [16B salt][4000B ciphertext][16B IV][64B HMAC]
  page N:  [4016B ciphertext][16B IV][64B HMAC]
解密后的 page 1 在开头补回 "SQLite format 3\\0"。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import struct
from pathlib import Path

try:
    from Crypto.Cipher import AES
except ImportError:
    try:
        # Debian 的 python3-pycryptodome 使用 Cryptodome 命名空间。
        from Cryptodome.Cipher import AES
    except ImportError:  # pragma: no cover - a11y 诊断不需要 AES
        AES = None  # type: ignore


def _require_aes() -> None:
    if AES is None:
        from wechat_linux.errors import DependencyError
        raise DependencyError('aes_dependency_unavailable')

PAGE_SZ = 4096
KEY_SZ = 32
SALT_SZ = 16
IV_SZ = 16
HMAC_SZ = 64
RESERVE_SZ = 80
SQLITE_HDR = b"SQLite format 3\x00"

WAL_HEADER_SZ = 32
WAL_FRAME_HEADER_SZ = 24
WAL_FRAME_SZ = WAL_FRAME_HEADER_SZ + PAGE_SZ


def derive_mac_key(enc_key: bytes, salt: bytes) -> bytes:
    mac_salt = bytes(b ^ 0x3A for b in salt)
    return hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=KEY_SZ)


def verify_page1(enc_key: bytes, page1: bytes) -> bool:
    """用 page 1 的 HMAC 验证 raw key 是否正确。"""
    if len(page1) < PAGE_SZ:
        return False
    salt = page1[:SALT_SZ]
    mac_key = derive_mac_key(enc_key, salt)
    hmac_data = page1[SALT_SZ : PAGE_SZ - RESERVE_SZ + IV_SZ]
    stored = page1[PAGE_SZ - HMAC_SZ : PAGE_SZ]
    calc = hmac.new(mac_key, hmac_data, hashlib.sha512)
    calc.update(struct.pack("<I", 1))
    return hmac.compare_digest(calc.digest(), stored)


def verify_page(enc_key: bytes, salt: bytes, page: bytes, pgno: int) -> bool:
    if len(page) != PAGE_SZ or pgno <= 0:
        return False
    start = SALT_SZ if pgno == 1 else 0
    mac_key = derive_mac_key(enc_key, salt)
    mac = hmac.new(mac_key, page[start:PAGE_SZ-HMAC_SZ], hashlib.sha512)
    mac.update(struct.pack('<I', pgno))
    return hmac.compare_digest(mac.digest(), page[-HMAC_SZ:])


def decrypt_page(enc_key: bytes, page: bytes, pgno: int) -> bytes:
    _require_aes()
    if len(page) < PAGE_SZ:
        page = page + b"\x00" * (PAGE_SZ - len(page))
    iv = page[PAGE_SZ - RESERVE_SZ : PAGE_SZ - RESERVE_SZ + IV_SZ]
    cipher = AES.new(enc_key, AES.MODE_CBC, iv)
    if pgno == 1:
        decrypted = cipher.decrypt(page[SALT_SZ : PAGE_SZ - RESERVE_SZ])
        return SQLITE_HDR + decrypted + b"\x00" * RESERVE_SZ
    decrypted = cipher.decrypt(page[: PAGE_SZ - RESERVE_SZ])
    return decrypted + b"\x00" * RESERVE_SZ


def decrypt_db_to_file(src: str | Path, dst: str | Path, enc_key: bytes) -> int:
    """解密整个数据库，返回页数。"""
    src = str(src)
    dst = str(dst)
    size = os.path.getsize(src)
    total_pages = size // PAGE_SZ + (1 if size % PAGE_SZ else 0)
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    tmp = dst + ".tmp"
    with open(src, "rb") as fin, open(tmp, "wb") as fout:
        for pgno in range(1, total_pages + 1):
            page = fin.read(PAGE_SZ)
            if not page:
                break
            fout.write(decrypt_page(enc_key, page, pgno))
    os.replace(tmp, dst)
    return total_pages


def apply_wal(
    wal_path: str | Path, plain_path: str | Path, enc_key: bytes, salt: bytes | None = None
) -> int:
    """把 WAL 中当前周期的 frame patch 到已解密的数据库副本。

    只回放当前周期最后一次提交之前的帧；支持数据库增长/收缩。
    当前桥接总是传入数据库 salt，用 SQLCipher 页 HMAC 校验密文及页号。
    """
    wal_path = str(wal_path)
    plain_path = str(plain_path)
    if not os.path.exists(wal_path) or os.path.getsize(wal_path) <= WAL_HEADER_SZ:
        return 0
    wal_size = os.path.getsize(wal_path)
    frames = []
    committed = final_pages = 0
    with open(wal_path, "rb") as wf:
        hdr = wf.read(WAL_HEADER_SZ)
        if len(hdr) < WAL_HEADER_SZ:
            return 0
        magic, version, page_size = struct.unpack('>III', hdr[:12])
        if magic not in (0x377f0682, 0x377f0683) or version != 3007000 or page_size != PAGE_SZ:
            raise ValueError('不支持的 WAL 格式')
        salt1, salt2 = struct.unpack(">II", hdr[16:24])
        while wf.tell() + WAL_FRAME_SZ <= wal_size:
            fh = wf.read(WAL_FRAME_HEADER_SZ)
            if len(fh) < WAL_FRAME_HEADER_SZ:
                break
            pgno = struct.unpack(">I", fh[0:4])[0]
            f_salt1, f_salt2 = struct.unpack(">II", fh[8:16])
            enc_page = wf.read(PAGE_SZ)
            if len(enc_page) < PAGE_SZ:
                break
            if pgno == 0 or pgno > 1_000_000:
                break
            if f_salt1 != salt1 or f_salt2 != salt2:
                break  # 当前周期已结束，后面是上一轮遗留的帧
            if salt is not None and not verify_page(enc_key, salt, enc_page, pgno):
                raise ValueError('WAL 加密页 HMAC 校验失败，取消合并')
            frames.append((pgno, enc_page))
            db_pages = struct.unpack('>I', fh[4:8])[0]
            if db_pages:
                if db_pages > 1_000_000:
                    raise ValueError('WAL 提交大小异常')
                committed, final_pages = len(frames), db_pages
    if not committed:
        return 0
    patched = 0
    with open(plain_path, 'r+b') as df:
        df.truncate(final_pages * PAGE_SZ)
        for pgno, enc_page in frames[:committed]:
            if pgno > final_pages:
                continue
            offset = (pgno - 1) * PAGE_SZ
            df.seek(offset)
            df.write(decrypt_page(enc_key, enc_page, pgno))
            patched += 1
    return patched


def decrypt_db_to_bytes(src: str | Path, enc_key: bytes) -> bytes:
    """小数据库（session/contact）直接解密到内存。"""
    src = str(src)
    size = os.path.getsize(src)
    total_pages = size // PAGE_SZ + (1 if size % PAGE_SZ else 0)
    out = bytearray()
    with open(src, "rb") as fin:
        for pgno in range(1, total_pages + 1):
            page = fin.read(PAGE_SZ)
            if not page:
                break
            out += decrypt_page(enc_key, page, pgno)
    return bytes(out)
