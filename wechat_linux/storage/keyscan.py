"""从运行中的微信进程内存提取 SQLCipher raw key。

原理：WCDB 在内存中缓存 `x'<64hex_enc_key><32hex_salt>'`。
扫描 /proc/<pid>/mem，匹配该模式，再用 page 1 的 HMAC 校验。

需要 root 或 CAP_SYS_PTRACE。仅用于读取本机、已登录的自己的账号数据。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

from wechat_linux.storage import crypto

HEX_RE = re.compile(rb"x'([0-9a-fA-F]{64,192})'")
CHUNK = 16 * 1024 * 1024
OVERLAP = 256

# WCDB cipher context 特征（agent-wechat 对 4.1.13 的做法）
# reserve_sz=32, iv_sz=16, hmac_sz=16, page_sz=4096
CIPHER_CTX_PATTERN = bytes(
    [0x20, 0x00, 0x00, 0x00,
     0x10, 0x00, 0x00, 0x00,
     0x10, 0x00, 0x00, 0x00,
     0x00, 0x10, 0x00, 0x00]
)
# 4.1.13.23 arm64/x86_64 共用的 DB key XOR 掩码。4.1.13.26 可能相同；
# 无法确定时同时尝试“无掩码”和“已知掩码”。
DB_XOR_MASKS = [
    None,
    bytes.fromhex(
        "b8ec38291bd59c963f4654d8f9d7437e"
        "1acc81a6cad6313c7bd0f5e73238d4af"
    ),
]
KNOWN_COMMS = {"wechat", "wechatappex", "weixin"}
INTERPRETERS = ("python", "bash", "sh", "zsh", "node", "perl", "ruby")
SKIP_MAPS = {"[vdso]", "[vsyscall]", "[vvar]"}
SKIP_PATH_PREFIXES = ("/usr/lib/", "/lib/", "/usr/share/")


def _readlink(path: str) -> str:
    try:
        return os.path.realpath(os.readlink(path))
    except OSError:
        return ""


def find_wechat_pids() -> list[tuple[int, int]]:
    """返回 [(pid, rss_kb)]，按内存从大到小。"""
    found: list[tuple[int, int]] = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        pid = int(name)
        if pid == os.getpid():
            continue
        try:
            with open(f"/proc/{pid}/comm", encoding="utf-8", errors="ignore") as f:
                comm = f.read().strip()
            if comm.lower() in KNOWN_COMMS:
                ok = True
            else:
                exe = os.path.basename(_readlink(f"/proc/{pid}/exe"))
                if any(exe.lower().startswith(p) for p in INTERPRETERS):
                    ok = False
                else:
                    ok = "wechat" in exe.lower() or "weixin" in exe.lower()
            if not ok:
                continue
            with open(f"/proc/{pid}/statm", encoding="utf-8") as f:
                rss_kb = int(f.read().split()[1]) * 4
            found.append((pid, rss_kb))
        except (OSError, ValueError, IndexError):
            continue
    found.sort(key=lambda x: x[1], reverse=True)
    return found


def collect_db_files(db_dir: str | Path):
    db_dir = str(db_dir)
    dbs = []  # (rel, abs, size, salt_hex, page1)
    salt_to_dbs: dict[str, list[str]] = {}
    for root, _dirs, files in os.walk(db_dir):
        for name in files:
            if not name.endswith(".db"):
                continue
            path = os.path.join(root, name)
            try:
                size = os.path.getsize(path)
                if size < crypto.PAGE_SZ:
                    continue
                with open(path, "rb") as f:
                    page1 = f.read(crypto.PAGE_SZ)
            except OSError:
                continue
            rel = os.path.relpath(path, db_dir).replace(os.sep, "/")
            salt_hex = page1[: crypto.SALT_SZ].hex()
            dbs.append((rel, path, size, salt_hex, page1))
            salt_to_dbs.setdefault(salt_hex, []).append(rel)
    return dbs, salt_to_dbs


def _readable_regions(pid: int):
    regions = []
    with open(f"/proc/{pid}/maps", encoding="utf-8") as f:
        for line in f:
            parts = line.split()
            if len(parts) < 2 or "r" not in parts[1]:
                continue
            if len(parts) >= 6:
                mapping = parts[5]
                if mapping in SKIP_MAPS:
                    continue
                low = mapping.lower()
                if (
                    mapping.startswith(SKIP_PATH_PREFIXES)
                    and "wcdb" not in low
                    and "wechat" not in low
                    and "weixin" not in low
                ):
                    continue
            try:
                start_s, end_s = parts[0].split("-")
                start = int(start_s, 16)
                size = int(end_s, 16) - start
            except ValueError:
                continue
            if 0 < size < 500 * 1024 * 1024:
                regions.append((start, size))
    return regions


def _check_permissions() -> None:
    if os.geteuid() == 0:
        return
    try:
        with open("/proc/self/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("CapEff:"):
                    cap = int(line.split(":", 1)[1].strip(), 16)
                    if cap & (1 << 19):  # CAP_SYS_PTRACE
                        return
                    break
    except (OSError, ValueError):
        pass
    raise SystemExit(
        "读取微信进程内存需要 root 或 CAP_SYS_PTRACE。\n"
        "请用：sudo python3 -m wxbridge scan-keys\n"
        "如果仍然失败，临时执行：sudo sysctl kernel.yama.ptrace_scope=0"
    )


def _try_candidate(
    hex_str: str,
    dbs,
    remaining: set[str],
    key_map: dict[str, str],
    where: str,
) -> tuple[str, str] | None:
    """尝试一个 hex 串，返回 (salt, enc_key_hex) 或 None。"""
    if len(hex_str) == 96:
        key_hex, salt_hex = hex_str[:64], hex_str[64:]
        candidates = [(salt_hex, key_hex)] if salt_hex in remaining else []
    elif len(hex_str) == 64:
        candidates = [(salt, hex_str) for salt in remaining]
    elif len(hex_str) > 96 and len(hex_str) % 2 == 0:
        key_hex, salt_hex = hex_str[:64], hex_str[-32:]
        candidates = [(salt_hex, key_hex)] if salt_hex in remaining else []
    else:
        return None

    for salt_hex, key_hex in candidates:
        try:
            enc_key = bytes.fromhex(key_hex)
        except ValueError:
            continue
        for rel, _path, _size, salt, page1 in dbs:
            if salt != salt_hex:
                continue
            if crypto.verify_page1(enc_key, page1):
                key_map[salt_hex] = key_hex
                remaining.discard(salt_hex)
                print(f"  [FOUND] {rel}  salt={salt_hex[:16]}…  ({where})", flush=True)
                return salt_hex, key_hex
    return None


def scan_pid(pid: int, dbs, salt_to_dbs, remaining, key_map, show_keys=False) -> int:
    regions = _readable_regions(pid)
    total = sum(size for _base, size in regions)
    print(f"[*] 扫描 PID={pid}，{total / 1024 / 1024:.0f} MB，{len(regions)} 个可读区域")
    scanned = 0
    matches = 0
    try:
        mem = open(f"/proc/{pid}/mem", "rb", buffering=0)
    except OSError as exc:
        print(f"[WARN] 无法打开 /proc/{pid}/mem：{exc}")
        return 0

    t0 = time.time()
    try:
        for base, size in regions:
            offset = 0
            while offset < size and remaining:
                length = min(CHUNK, size - offset)
                try:
                    mem.seek(base + offset)
                    data = mem.read(length)
                except (OSError, ValueError):
                    break
                if not data:
                    break
                scanned += len(data)
                for m in HEX_RE.finditer(data):
                    matches += 1
                    found = _try_candidate(
                        m.group(1).decode("ascii", "ignore"),
                        dbs,
                        remaining,
                        key_map,
                        f"pid={pid} addr=0x{base + offset + m.start():x}",
                    )
                    if found and show_keys:
                        print(f"    enc_key={found[1]}", flush=True)
                    if not remaining:
                        break
                offset += length
                if length > OVERLAP:
                    offset -= OVERLAP
                elapsed = time.time() - t0
                if scanned and int(elapsed) and int(elapsed) % 10 == 0 and offset % (CHUNK * 4) == 0:
                    pct = scanned / total * 100 if total else 100
                    print(
                        f"  [{pct:5.1f}%] 已扫描 {scanned / 1024 / 1024:.0f} MB，"
                        f"匹配 {matches} 个模式，找到 {len(key_map)}/{len(salt_to_dbs)} 个 salt",
                        flush=True,
                    )
            if not remaining:
                break
    finally:
        mem.close()
    print(
        f"[*] PID={pid} 完成：{scanned / 1024 / 1024:.0f} MB，"
        f"{matches} 个 x'…' 模式，用时 {time.time() - t0:.1f}s"
    )
    return matches


def _rw_regions(pid: int):
    """所有可读写内存区域（cipher context 在堆/匿名映射里）。"""
    regions = []
    try:
        with open(f"/proc/{pid}/maps", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 2 or not parts[1].startswith("rw"):
                    continue
                try:
                    start_s, end_s = parts[0].split("-")
                    start = int(start_s, 16)
                    size = int(end_s, 16) - start
                except ValueError:
                    continue
                if 0 < size < 500 * 1024 * 1024:
                    regions.append((start, size))
    except OSError:
        pass
    return regions


def _read_region(mem_fd: int, start: int, size: int) -> bytes:
    try:
        return os.pread(mem_fd, size, start)
    except OSError:
        # 区域中间可能有不可读页，退化成小块读取
        out = bytearray()
        step = 1024 * 1024
        for off in range(0, size, step):
            try:
                out += os.pread(mem_fd, min(step, size - off), start + off)
            except OSError:
                out += b"\x00" * min(step, size - off)
        return bytes(out)


def _is_key_like(raw: bytes) -> bool:
    if len(raw) != 32:
        return False
    nonzero = sum(1 for b in raw if b != 0)
    unique = len(set(raw))
    printable = sum(1 for b in raw if 0x20 <= b <= 0x7E)
    return nonzero >= 12 and unique >= 8 and printable <= 28


def _read_u64(mem_fd: int, addr: int) -> int | None:
    try:
        raw = os.pread(mem_fd, 8, addr)
        if len(raw) != 8:
            return None
        return int.from_bytes(raw, "little")
    except OSError:
        return None


def _valid_ptr(value: int | None) -> bool:
    return value is not None and 0x10000 < value < 0x7FFFFFFFFFFF


def scan_cipher_ctx(
    pid: int,
    dbs,
    salt_to_dbs,
    remaining: set[str],
    key_map: dict[str, str],
    show_keys: bool = False,
) -> int:
    """4.1.13 路线：找 WCDB cipher context，沿指针链找 32 字节 key。

    返回收集到的候选数量。已知 key 可能被 XOR 掩码处理，所以每个候选
    会分别用“无掩码”和已知掩码尝试。
    """
    if not hasattr(os, "pread"):
        return 0
    regions = _rw_regions(pid)
    total = sum(size for _start, size in regions)
    print(
        f"[*] cipher-context 扫描 PID={pid}，{total / 1024 / 1024:.0f} MB，"
        f"{len(regions)} 个 rw 区域"
    )
    ctxs: set[int] = set()
    try:
        mem = open(f"/proc/{pid}/mem", "rb", buffering=0)
    except OSError as exc:
        print(f"[WARN] 无法打开 /proc/{pid}/mem：{exc}")
        return 0
    try:
        fd = mem.fileno()
        for start, size in regions:
            if len(ctxs) > 50_000:
                break
            data = _read_region(fd, start, size)
            pos = 0
            while True:
                idx = data.find(CIPHER_CTX_PATTERN, pos)
                if idx < 0:
                    break
                ctxs.add(start + idx)
                pos = idx + 1
        print(f"[*] 找到 {len(ctxs)} 个 cipher context")

        candidates: set[bytes] = set()

        def add_raw(addr: int) -> None:
            try:
                raw = os.pread(fd, 32, addr)
            except OSError:
                return
            if len(raw) == 32 and _is_key_like(raw):
                candidates.add(raw)

        for ctx in ctxs:
            for off in range(-256, 513, 8):
                add_raw(ctx + off)
            for off in range(-128, 257, 8):
                val = _read_u64(fd, ctx + off)
                if not _valid_ptr(val):
                    continue
                assert val is not None
                for koff in range(0, 129, 8):
                    add_raw(val + koff)
                for poff in range(0, 65, 8):
                    val2 = _read_u64(fd, val + poff)
                    if not _valid_ptr(val2):
                        continue
                    assert val2 is not None
                    add_raw(val2)
                    for koff2 in range(8, 65, 8):
                        add_raw(val2 + koff2)
    finally:
        mem.close()

    print(f"[*] cipher-context 候选：{len(candidates)} 个 32 字节值")
    if not candidates:
        return 0

    # salt -> page1；只测试还没拿到 key 的 salt
    by_salt = {}
    for _rel, _path, _size, salt, page1 in dbs:
        if salt in remaining:
            by_salt[salt] = page1

    tested = 0
    for raw in candidates:
        variants = [raw]
        for mask in DB_XOR_MASKS:
            if mask:
                variants.append(bytes(a ^ b for a, b in zip(raw, mask)))
        for key in variants:
            for salt, page1 in by_salt.items():
                if salt not in remaining:
                    continue
                tested += 1
                if crypto.verify_page1(key, page1):
                    key_map[salt] = key.hex()
                    remaining.discard(salt)
                    print(
                        f"  [FOUND] salt={salt[:16]}…  "
                        f"({', '.join(salt_to_dbs[salt][:2])})",
                        flush=True,
                    )
                    if show_keys:
                        print(f"    enc_key={key.hex()}", flush=True)
                    break
    print(f"[*] cipher-context 测试了 {tested} 次候选，命中 {len(key_map)} 个 salt")
    return len(candidates)


def extract_keys(
    db_dir: str | Path,
    out_file: str | Path,
    pid: int | None = None,
    show_keys: bool = False,
) -> dict:
    _check_permissions()
    dbs, salt_to_dbs = collect_db_files(db_dir)
    if not dbs:
        raise SystemExit(f"在 {db_dir} 下没有找到 .db 文件")
    print(f"找到 {len(dbs)} 个数据库，{len(salt_to_dbs)} 个不同 salt")
    for salt, rels in sorted(salt_to_dbs.items(), key=lambda x: len(x[1]), reverse=True)[:10]:
        print(f"  salt {salt[:16]}…  ->  {', '.join(rels[:3])}{' …' if len(rels) > 3 else ''}")

    pids = [(pid, 0)] if pid else find_wechat_pids()
    if not pids:
        raise SystemExit("没有找到运行中的微信进程（comm=wechat）。请先登录并保持微信运行。")
    if pid:
        print(f"[*] 使用指定 PID={pid}")
    else:
        for p, rss in pids:
            print(f"[+] 微信进程 PID={p}，RSS≈{rss / 1024:.0f} MB")

    remaining = set(salt_to_dbs)
    key_map: dict[str, str] = {}
    # 先走 4.1.13 的 cipher-context 路线，再走旧的 x'<key><salt>' 路线
    for p, _rss in pids:
        if not remaining:
            break
        try:
            scan_cipher_ctx(p, dbs, salt_to_dbs, remaining, key_map, show_keys=show_keys)
        except Exception as exc:
            print(f"[WARN] cipher-context 扫描失败：{exc}")
    for p, _rss in pids:
        if not remaining:
            break
        scan_pid(p, dbs, salt_to_dbs, remaining, key_map, show_keys=show_keys)

    result: dict = {}
    for rel, path, size, salt_hex, _page1 in dbs:
        if salt_hex in key_map:
            result[rel] = {
                "enc_key": key_map[salt_hex],
                "salt": salt_hex,
                "size_mb": round(size / 1024 / 1024, 1),
            }
    missing = [rel for rel, _p, _s, salt, _pg in dbs if salt not in key_map]
    print(f"\n结果：{len(result)}/{len(dbs)} 个数据库拿到密钥")
    if missing:
        print("未拿到密钥的数据库：")
        for rel in missing[:20]:
            print(f"  MISS {rel}")
        if len(missing) > 20:
            print(f"  … 还有 {len(missing) - 20} 个")

    if not result:
        raise SystemExit(
            "一个密钥都没提取到。请确认：\n"
            "  1) 微信已登录且处于已解锁/已进入主界面状态；\n"
            "  2) 使用 root 运行；\n"
            "  3) db_dir 指向当前登录账号的 db_storage；\n"
            "  4) 如仍失败，可临时 sudo sysctl kernel.yama.ptrace_scope=0 后重试。"
        )

    result["_db_dir"] = str(db_dir)
    result["_extracted_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    out_file = Path(out_file)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(out_file, 0o600)
    except OSError:
        pass
    print(f"密钥已写入 {out_file}（权限 600）")
    return result
