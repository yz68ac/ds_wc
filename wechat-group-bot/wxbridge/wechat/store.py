"""读取微信本地数据库。

- 定位当前账号的 db_storage
- 按需解密 session.db / contact.db / message_*.db（带 WAL patch）
- 解析群聊消息与发送者昵称
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any

from wxbridge.wechat import crypto
from wxbridge.wechat.keyscan import find_wechat_pids
from wxbridge.storage.memoryfs import require_memory_directory
from wxbridge.wechat.references import image_md5, parse_quoted_reply

MSG_TYPE_LABEL = {
    1: "",
    3: "[图片]",
    34: "[语音]",
    42: "[名片]",
    43: "[视频]",
    47: "[表情]",
    48: "[位置]",
    49: "",
    50: "[通话]",
    10000: "[系统消息]",
    10002: "[撤回]",
}


class StoreError(RuntimeError):
    pass


def parse_mentioned_users(source: str) -> list[str] | None:
    if not source.strip():
        return None
    try:
        root = ET.fromstring(source)
    except ET.ParseError:
        return None
    users = []
    for node in root.iter():
        if node.tag.rsplit('}', 1)[-1] == 'atuserlist':
            users.extend(u.strip() for u in (node.text or '').split(',') if u.strip())
    return users


def _mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _readlink(path: str) -> str:
    try:
        return os.path.realpath(os.readlink(path))
    except OSError:
        return ""


def discover_db_dir(preferred: str = "") -> str:
    """定位当前登录账号的 db_storage。"""
    if preferred:
        path = os.path.expanduser(preferred)
        if os.path.isdir(path):
            return path
        raise StoreError(f"配置的 db_dir 不存在：{path}")

    # 优先从微信进程打开的文件里找正在使用的目录
    try:
        for pid, _rss in find_wechat_pids():
            fd_dir = f"/proc/{pid}/fd"
            found: list[str] = []
            try:
                for fd in os.listdir(fd_dir):
                    target = _readlink(os.path.join(fd_dir, fd))
                    if "xwechat_files" in target and "db_storage" in target:
                        # .../<wxid>/db_storage/xxx/yyy.db
                        idx = target.find("db_storage")
                        found.append(target[: idx + len("db_storage")])
            except OSError:
                pass
            if found:
                return os.path.commonpath(found)
    except Exception:
        pass

    home = str(Path.home())
    patterns = [
        os.path.join(home, "Documents", "xwechat_files", "*", "db_storage"),
        os.path.join(home, "xwechat_files", "*", "db_storage"),
        os.path.join(home, "文档", "xwechat_files", "*", "db_storage"),
    ]
    candidates: list[str] = []
    for pattern in patterns:
        candidates.extend(glob.glob(pattern))
    candidates = [c for c in candidates if os.path.isdir(c)]
    if not candidates:
        raise StoreError(
            "找不到微信数据目录。请确认已登录微信，或在 config.json 里设置 db_dir，"
            "例如 ~/Documents/xwechat_files/wxid_xxx/db_storage"
        )
    # 选最近有写入的
    candidates.sort(key=lambda d: _mtime(os.path.join(d, "session", "session.db")), reverse=True)
    return candidates[0]


def _decompress_zstd(data: bytes) -> str | None:
    try:
        import zstandard as zstd  # type: ignore

        return zstd.ZstdDecompressor().decompress(data).decode("utf-8", "replace")
    except Exception:
        pass
    exe = shutil.which("zstd")
    if exe:
        try:
            proc = subprocess.run(
                [exe, "-dc", "-q"], input=data, capture_output=True, timeout=10
            )
            if proc.returncode == 0:
                return proc.stdout.decode("utf-8", "replace")
        except Exception:
            pass
    return None


def _strip_xml_title(text: str) -> str:
    """从 type=49 的 XML 里尽量取标题/内容。"""
    import re

    for tag in ("title", "des", "content"):
        m = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.S)
        if m:
            value = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", m.group(1), flags=re.S)
            return value.strip()[:300]
    return "[链接/文件]"


class WeChatStore:
    def __init__(self, db_dir: str, keys_file: str, decrypted_dir: str):
        self.db_dir = discover_db_dir(db_dir)
        self.keys_file = keys_file
        self.decrypted_dir = decrypted_dir
        try:
            require_memory_directory(decrypted_dir)
        except (ValueError, OSError) as exc:
            raise StoreError(str(exc)) from exc
        self.keys = self._load_keys()
        self._mtimes: dict[str, tuple[float, float]] = {}
        self._wal_mtimes: dict[str, float] = {}

    # ---------- 基础 ----------
    def _load_keys(self) -> dict[str, Any]:
        path = Path(self.keys_file)
        if not path.exists():
            raise StoreError(
                f"密钥文件不存在：{path}\n请先运行：sudo python3 -m wxbridge scan-keys"
            )
        data = json.loads(path.read_text(encoding="utf-8"))
        return {k: v for k, v in data.items() if not k.startswith("_")}

    @property
    def self_wxid(self) -> str:
        if hasattr(self, "_account_username"):
            return self._account_username
        name = Path(self.db_dir).parent.name
        if not name.startswith("wxid_"):
            return ""
        contacts = self.contacts()
        # 实测账号目录附带后缀，不能直接当作微信 username。
        candidates = (name, name.rsplit("_", 1)[0])
        self._account_username = next((c for c in candidates if c in contacts), "")
        return self._account_username

    def _key_for(self, rel: str) -> dict[str, Any] | None:
        rel = rel.replace(os.sep, "/")
        keys = self.keys
        for candidate in (rel, rel.replace("/", "\\")):
            if candidate in keys:
                return keys[candidate]
        return None

    def _plain_path(self, rel: str) -> Path:
        return Path(self.decrypted_dir) / rel

    def refresh(self, rel: str) -> Path:
        """确保 rel 对应的明文数据库是最新的，返回明文路径。"""
        rel = rel.replace(os.sep, "/")
        src = os.path.join(self.db_dir, rel)
        if not os.path.exists(src):
            raise StoreError(f"数据库不存在：{src}")
        info = self._key_for(rel)
        if not info:
            raise StoreError(
                f"缺少 {rel} 的密钥。请重新运行 scan-keys（注意 db_dir 要对应当前账号）。"
            )
        enc_key = bytes.fromhex(info["enc_key"])
        dst = self._plain_path(rel)
        wal = src + "-wal"
        db_mt = _mtime(src)
        wal_mt = _mtime(wal)
        prev = self._mtimes.get(rel)
        if dst.exists() and prev and prev[0] == db_mt and prev[1] == wal_mt:
            return dst
        t0 = time.time()
        if dst.exists() and prev and prev[0] == db_mt and prev[1] != wal_mt:
            crypto.apply_wal(wal, dst, enc_key, bytes.fromhex(info['salt']))
        else:
            crypto.decrypt_db_to_file(src, dst, enc_key)
            crypto.apply_wal(wal, dst, enc_key, bytes.fromhex(info['salt']))
        self._mtimes[rel] = (db_mt, wal_mt)
        cost = time.time() - t0
        if cost > 1.0:
            print(f"[store] 解密 {rel} 用时 {cost:.1f}s", flush=True)
        return dst

    def _connect(self, rel: str) -> sqlite3.Connection:
        path = self.refresh(rel)
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA temp_store=MEMORY')
        conn.execute('PRAGMA query_only=ON')
        return conn

    # ---------- 会话 / 联系人 ----------
    def sessions(self) -> dict[str, dict[str, Any]]:
        conn = self._connect("session/session.db")
        try:
            try:
                rows = conn.execute(
                    "SELECT username, unread_count, summary, last_timestamp, "
                    "last_msg_type, last_msg_sender, last_sender_display_name "
                    "FROM SessionTable WHERE last_timestamp > 0"
                ).fetchall()
            except sqlite3.OperationalError:
                rows = conn.execute("SELECT * FROM SessionTable").fetchall()
        finally:
            conn.close()
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            d = dict(row)
            username = d.get("username") or d.get("strUsername") or d.get("user_name")
            if not username:
                continue
            out[username] = {
                "unread": d.get("unread_count", 0) or 0,
                "summary": d.get("summary") or "",
                "timestamp": d.get("last_timestamp", 0) or 0,
                "msg_type": d.get("last_msg_type", 0) or 0,
                "sender": d.get("last_msg_sender") or "",
                "sender_name": d.get("last_sender_display_name") or "",
            }
        return out

    def contacts(self) -> dict[str, dict[str, str]]:
        conn = self._connect("contact/contact.db")
        try:
            rows = conn.execute("SELECT username, nick_name, remark FROM contact").fetchall()
        except sqlite3.OperationalError:
            rows = []
        finally:
            conn.close()
        out: dict[str, dict[str, str]] = {}
        for row in rows:
            d = dict(row)
            out[d.get("username") or ""] = {
                "nick_name": d.get("nick_name") or "",
                "remark": d.get("remark") or "",
            }
        return out

    @staticmethod
    def display_name(contacts: dict[str, dict[str, str]], username: str, fallback: str = "") -> str:
        c = contacts.get(username)
        if c:
            return c.get("remark") or c.get("nick_name") or fallback or username
        return fallback or username

    def resolve_chat(self, target: str) -> tuple[str, str]:
        """把群名/备注/wxid 解析成 (username, display_name)。"""
        target = (target or "").strip()
        if not target:
            raise StoreError("config.json 里 target.chat 为空")
        if target.endswith("@chatroom") or target.startswith("wxid_") or target.startswith("gh_"):
            contacts = self.contacts()
            return target, self.display_name(contacts, target, target)

        contacts = self.contacts()
        # 精确匹配备注/昵称
        for username, c in contacts.items():
            if target in (c.get("remark"), c.get("nick_name")):
                return username, self.display_name(contacts, username, target)
        # 包含匹配
        matches = [
            (username, c)
            for username, c in contacts.items()
            if target in (c.get("remark") or "") or target in (c.get("nick_name") or "")
        ]
        if len(matches) == 1:
            username, _ = matches[0]
            return username, self.display_name(contacts, username, target)
        if len(matches) > 1:
            names = "、".join(
                f"{self.display_name(contacts, u, u)}({u})" for u, _ in matches[:8]
            )
            raise StoreError(f"群名“{target}”匹配到多个联系人：{names}。请在 config 里直接写 wxid 或 @chatroom")

        # 再从会话列表找
        for username in self.sessions():
            if username == target:
                return username, self.display_name(contacts, username, target)
        raise StoreError(
            f"找不到“{target}”。请先把群聊在微信里打开一次，或在 contact.db 里确认群名；"
            "也可以直接填群 id（xxx@chatroom）。"
        )

    # ---------- 消息 ----------
    def _message_dbs(self) -> list[str]:
        root = os.path.join(self.db_dir, "message")
        try:
            names = [
                f"message/{n}"
                for n in os.listdir(root)
                if n.startswith("message_") and n.endswith(".db")
            ]
        except OSError:
            return []
        names.sort(key=lambda x: (x != "message/message_0.db", x))
        return names

    def message_table(self, chat_id: str) -> tuple[str, str]:
        """返回 (message DB rel, Msg_ 表名)。"""
        want = "Msg_" + hashlib.md5(chat_id.encode("utf-8")).hexdigest()
        dbs = self._message_dbs()
        for rel in dbs:
            try:
                conn = self._connect(rel)
                try:
                    row = conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (want,),
                    ).fetchone()
                finally:
                    conn.close()
                if row:
                    return rel, want
            except (StoreError, sqlite3.DatabaseError):
                continue

        # 回退：用 Name2Id 反查
        for rel in dbs:
            try:
                conn = self._connect(rel)
                try:
                    rows = conn.execute("SELECT user_name FROM Name2Id").fetchall()
                except sqlite3.OperationalError:
                    rows = []
                finally:
                    conn.close()
                for row in rows:
                    username = row["user_name"] if isinstance(row, sqlite3.Row) else row[0]
                    if username == chat_id:
                        table = "Msg_" + hashlib.md5(username.encode("utf-8")).hexdigest()
                        return rel, table
            except (StoreError, sqlite3.DatabaseError):
                continue
        raise StoreError(
            f"在 message_*.db 里找不到 {chat_id} 的消息表。"
            "可能该群还没有本地消息，或微信版本改了表结构。"
        )

    def _decode_content(self, content: Any, ct: Any) -> str:
        if content is None:
            return ""
        if isinstance(content, memoryview):
            content = content.tobytes()
        if isinstance(content, bytes):
            if ct == 4:
                text = _decompress_zstd(content)
                if text is None:
                    return "[无法解压的消息]"
                return text
            try:
                return content.decode("utf-8", "replace")
            except Exception:
                return "[二进制消息]"
        return str(content)

    def _parse_message(
        self, chat_id: str, row: sqlite3.Row, contacts: dict, senders: dict | None = None
    ) -> dict[str, Any] | None:
        d = dict(row)
        local_id = d.get("local_id") or 0
        raw_type = int(d.get("local_type") or 0)
        local_type = raw_type & 0xFFFFFFFF
        create_time = d.get("create_time") or 0
        content = self._decode_content(
            d.get("message_content"), d.get("WCDB_CT_message_content")
        )
        source = self._decode_content(d.get('source'), d.get('WCDB_CT_source'))
        is_group = chat_id.endswith("@chatroom")
        sender = (senders or {}).get(d.get("real_sender_id"), "")
        text = content
        if is_group and ":\n" in content:
            prefix, body = content.split(":\n", 1)
            prefix = prefix.strip()
            if prefix == sender or (not sender and (prefix in contacts or prefix.startswith("wxid_"))):
                sender, text = prefix, body
        reference = None
        picture_md5 = image_md5(text) if local_type == 3 else ""
        message_kind = "text" if local_type == 1 else "non_text"
        if local_type == 49 and text:
            quoted = parse_quoted_reply(text, raw_type)
            if quoted is not None:
                text, reference = quoted
                local_type, message_kind = 1, "quoted_text"
            else:
                text = _strip_xml_title(text)
        if local_type == 3:
            text = "[图片]"
        if local_type != 1 and not text:
            text = MSG_TYPE_LABEL.get(local_type, f"[type={local_type}]")
        sender_name = self.display_name(contacts, sender, sender) if sender else ""
        if text is None:
            text = ""
        return {
            "local_id": int(local_id),
            "type": int(local_type),
            "raw_type": raw_type,
            "message_kind": message_kind,
            "reference": reference,
            "image_md5": picture_md5,
            "server_id": str(d.get("server_id") or ""),
            "time": int(create_time),
            "time_source": "database",
            "sender": sender,
            "sender_name": sender_name,
            "text": text,
            "is_group": is_group,
            "mentioned_users": parse_mentioned_users(source) if is_group else [],
        }

    def _query_messages(
        self, chat_id: str, limit: int | None, after_local_id: int | None = None,
        time_window: tuple[int, int] | None = None,
        server_id: str | None = None,
    ) -> list[dict[str, Any]]:
        rel, table = self.message_table(chat_id)
        contacts = self.contacts()
        conn = self._connect(rel)
        try:
            available = {r['name'] for r in conn.execute(f"PRAGMA table_info([{table}])")}
            cols = "rowid, local_id, local_type, create_time, message_content"
            for optional in ("WCDB_CT_message_content", "real_sender_id", "source", "WCDB_CT_source", "server_id"):
                if optional in available:
                    cols += ", " + optional
            senders = {}
            if "real_sender_id" in available:
                senders = {r['rowid']: r['user_name'] for r in conn.execute("SELECT rowid,user_name FROM Name2Id")}
            where = []
            params: list[Any] = []
            if server_id is not None:
                if "server_id" not in available:
                    return []
                where.append("server_id = ?")
                params.append(server_id)
            if after_local_id is not None:
                where.append("local_id > ?")
                params.append(after_local_id)
            if time_window is not None:
                where.append("create_time >= ? AND create_time <= ?")
                params.extend(time_window)
            suffix = (" WHERE " + " AND ".join(where)) if where else ""
            suffix += (" ORDER BY local_id ASC" if after_local_id is not None
                       else " ORDER BY create_time DESC, local_id DESC")
            if limit is not None:
                suffix += " LIMIT ?"
                params.append(limit)
            rows = conn.execute(f"SELECT {cols} FROM [{table}]" + suffix, params).fetchall()
        finally:
            conn.close()
        parsed = [self._parse_message(chat_id, row, contacts, senders) for row in rows]
        parsed = [p for p in parsed if p is not None]
        if after_local_id is None:
            parsed.reverse()
        return parsed

    def image_binding(self, chat_id: str, reference: dict) -> dict:
        """主进程查当前群消息及附件索引；仅返回本账号本地加密文件描述。"""
        binding = dict(account_root=str(Path(self.db_dir).parent), account=self.self_wxid,
                       home=str(Path.home()), candidates=[])
        digest = reference.get("image_md5") or ""
        server_id = str(reference.get("server_id") or "")
        proven = False
        try:
            if reference.get("chat_id") and reference["chat_id"] != chat_id:
                return {**binding, "error": "image_reference_wrong_chat"}
            if re.fullmatch(r"[1-9][0-9]{0,19}", server_id):
                originals = self._query_messages(chat_id, 2, server_id=server_id)
                if originals:
                    original = originals[0]
                    if (original["type"] != 3 or not original.get("image_md5")
                            or (digest and digest != original["image_md5"])
                            or (reference.get("sender") and original.get("sender") != reference["sender"])):
                        return {**binding, "error": "image_reference_mismatch"}
                    digest, proven = original["image_md5"], True
            if not re.fullmatch(r"[0-9a-f]{32}", digest):
                return {**binding, "error": "image_identifier_missing"}
            conn = self._connect("hardlink/hardlink.db")
            try:
                rows = conn.execute(
                    "SELECT i.file_name,d1.username AS bucket,d2.username AS month "
                    "FROM image_hardlink_info_v4 i LEFT JOIN dir2id d1 ON i.dir1=d1.rowid "
                    "LEFT JOIN dir2id d2 ON i.dir2=d2.rowid WHERE i.md5 IN (?,?,?) LIMIT 20",
                    (digest, digest.encode("ascii"), bytes.fromhex(digest))).fetchall()
            finally:
                conn.close()
            chat_hash = hashlib.md5(chat_id.encode()).hexdigest()
            for row in rows:
                filename, bucket, month = row["file_name"], row["bucket"], row["month"]
                if (not isinstance(filename, str) or not re.fullmatch(r"[0-9a-fA-F]{32}(?:_[ht])?\.dat", filename)
                        or not isinstance(bucket, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", bucket)
                        or not isinstance(month, str) or not re.fullmatch(r"20[0-9]{2}-(?:0[1-9]|1[0-2])", month)
                        or (bucket.lower() != chat_hash and not proven)):
                    continue
                stem = re.sub(r"_[ht]$", "", filename[:-4])
                base = f"msg/attach/{bucket}/{month}/Img/"
                candidates = [(base+stem+"_h.dat", "original"), (base+stem+".dat", "original"),
                              (f"cache/{month}/Message/{chat_hash}/Bubble/{stem}_b.dat", "preview"),
                              (base+stem+"_t.dat", "thumbnail")]
                for relative, quality in candidates:
                    item = dict(relative=relative, quality=quality)
                    if item not in binding["candidates"]:
                        binding["candidates"].append(item)
            if not binding["candidates"]:
                binding["error"] = "local_image_index_missing"
        except (StoreError, sqlite3.DatabaseError, OSError, ValueError):
            binding["error"] = "local_image_index_unavailable"
        return binding

    def recent_messages(self, chat_id: str, limit: int = 20) -> list[dict[str, Any]]:
        return self._query_messages(chat_id, max(1, limit))

    def messages_in_window(self, chat_id: str, start: int, end: int) -> list[dict[str, Any]]:
        if start > end or end <= 0:
            raise StoreError("无效的消息时间窗口")
        return self._query_messages(chat_id, None, time_window=(start, end))

    def messages_after(self, chat_id: str, after_local_id: int, limit: int = 50):
        return self._query_messages(chat_id, limit, after_local_id)

    def last_local_id(self, chat_id: str) -> int:
        rel, table = self.message_table(chat_id)
        conn = self._connect(rel)
        try:
            row = conn.execute(f"SELECT COALESCE(MAX(local_id), 0) FROM [{table}]").fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()

    def session_of(self, chat_id: str) -> dict[str, Any] | None:
        return self.sessions().get(chat_id)

    def describe(self) -> str:
        lines = [
            f"数据目录: {self.db_dir}",
            f"当前账号: {self.self_wxid or '未知'}",
            f"密钥数量: {len(self.keys)}",
        ]
        try:
            sessions = self.sessions()
            lines.append(f"会话数量: {len(sessions)}")
        except Exception as exc:
            lines.append(f"会话读取失败: {exc}")
        return "\n".join(lines)


def format_time(ts: int) -> str:
    try:
        return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M:%S")
    except (OSError, ValueError):
        return str(ts)
