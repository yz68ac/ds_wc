"""Validated file edits; the web application never opens a WeChat database or Journal."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import uuid

from wxbridge.config import DEFAULTS, load_config, resolve_config
from wxbridge.config_merge import deep_merge
from wxbridge.instances import CHAT_ID, GROUP_FIELDS, INSTANCE_ID, validate_account_fields


class ManagementError(Exception):
    def __init__(self, code, message, status=400):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def revision(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def no_secrets(value):
    """Connection credentials belong in provider configuration, never instance JSON."""
    if isinstance(value, dict):
        for key, item in value.items():
            if re.search(r'api.?key|token|password|secret|credential|authorization', key, re.I) and key not in {'max_tokens', 'thinking_budget_tokens'}:
                raise ManagementError('credentials_in_instance', '实例配置不能包含密钥，请使用服务器上的 provider 配置。')
            no_secrets(item)
    elif isinstance(value, list):
        for item in value:
            no_secrets(item)
    elif isinstance(value, float) and not math.isfinite(value):
        raise ManagementError('invalid_number', '配置中的数字必须是有限数值。')


class ConfigRepository:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.lock = threading.RLock()
        self.config_file = self.root/'config.json'

    def _safe(self, path):
        path = Path(path)
        if not path.resolve().is_relative_to(self.root):
            raise ManagementError('unsafe_path', '文件必须位于项目目录内。', 403)
        current = path
        while current != self.root:
            if current.is_symlink() or (hasattr(current, 'is_junction') and current.is_junction()):
                raise ManagementError('unsafe_path', '不能编辑链接文件。', 403)
            current = current.parent
        return path

    def _read(self, path, limit=256*1024):
        path = self._safe(path)
        if not path.is_file():
            raise ManagementError('file_missing', '配置文件尚未创建。', 404)
        if path.stat().st_size > limit:
            raise ManagementError('file_too_large', '文件超过允许大小。', 413)
        return path.read_bytes()

    def _json(self, path):
        raw = self._read(path)
        try:
            data = json.loads(raw.decode('utf-8-sig'))
        except (ValueError, UnicodeError):
            raise ManagementError('invalid_json', '配置不是有效的 UTF-8 JSON。') from None
        if not isinstance(data, dict):
            raise ManagementError('invalid_json', '配置必须是 JSON 对象。')
        return data, revision(raw)

    def _directory(self, identifier):
        if not INSTANCE_ID.fullmatch(identifier):
            raise ManagementError('invalid_instance_id', '实例 ID 只能使用字母、数字、下划线和连字符，最多48个字符。')
        return self._safe(self.root/'instance'/identifier)

    def _write(self, path, data):
        path = self._safe(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = path.stat() if path.exists() else None
        fd, name = tempfile.mkstemp(prefix='.web-', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(name, (info.st_mode & 0o777) if info else 0o600)
            if info and hasattr(os, 'chown'):
                os.chown(name, info.st_uid, info.st_gid)
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def account(self):
        data, _ = self._json(self.config_file)
        try:
            validate_account_fields(data)
            return resolve_config(data, self.config_file, validate_role_models=False)
        except (ValueError, TypeError, KeyError):
            raise ManagementError('invalid_account_config', '账号配置无效，请先检查服务器 config.json。') from None

    def list(self):
        with self.lock:
            directory = self._safe(self.root/'instance')
            result = []
            for path in sorted(directory.glob('*/config.json')) if directory.exists() else []:
                item = self.get(path.parent.name)
                data = item['config']
                result.append(dict(id=item['id'], chat=data.get('chat', ''), enabled=data.get('enabled', True),
                                   kind='group' if data.get('chat', '').endswith('@chatroom') else 'private',
                                   reply_model=item['effective']['models']['reply']['model'], revision=item['revision']))
            return result

    def _validate(self, identifier, data):
        if not isinstance(data, dict) or set(data)-GROUP_FIELDS-{'chat', 'enabled'}:
            raise ManagementError('invalid_instance_fields', '实例配置含有不支持的字段。')
        no_secrets(data)
        chat = data.get('chat', '')
        if type(data.get('enabled', True)) is not bool or not isinstance(chat, str) or not CHAT_ID.fullmatch(chat):
            raise ManagementError('invalid_chat', '请填写真实 wxid 或以 @chatroom 结尾的群 ID，并使用布尔启用开关。')
        for section in GROUP_FIELDS-{'system_prompt_file'}:
            if section in data and not isinstance(data[section], dict):
                raise ManagementError('invalid_section', '实例各配置段必须是 JSON 对象。')
        for section, forbidden in [('target', {'chat'}), ('queue', {'file'}), ('memory', {'directory'})]:
            if set(data.get(section, {})) & forbidden:
                raise ManagementError('account_resource_override', '实例不能覆盖账号数据库、队列和业务数据目录。')
        prompt = data.get('system_prompt_file', 'system.md')
        if not isinstance(prompt, str) or not prompt.strip() or Path(prompt).is_absolute():
            raise ManagementError('invalid_prompt_path', '系统提示词必须使用实例目录内的相对文件名。')
        target = self._safe(self._directory(identifier)/prompt)
        if not target.resolve().is_relative_to(self._directory(identifier).resolve()):
            raise ManagementError('invalid_prompt_path', '系统提示词必须位于所属实例目录内。')
        parent = self._safe(self.root/'instance')
        others = list(parent.glob('*/config.json')) if parent.exists() else []
        if not (self._directory(identifier)/'config.json').exists() and len(others) >= 16:
            raise ManagementError('instance_limit', '最多支持16个实例。')
        for path in others:
            if path.parent.name == identifier:
                continue
            other, _ = self._json(path)
            if path.parent.name.casefold() == identifier.casefold() or other.get('chat') == chat:
                raise ManagementError('duplicate_instance', '实例 ID 或会话 ID 已存在。', 409)
        account = self.account()
        override = {key: deepcopy(value) for key, value in data.items() if key in GROUP_FIELDS}
        override['system_prompt_file'] = str(target)
        merged = deep_merge(account, override)
        merged['target']['chat'] = chat
        try:
            effective = resolve_config(merged, self._directory(identifier)/'config.json', environment=False,
                                       validate_role_models=data.get('enabled',True))
        except (ValueError, TypeError, KeyError):
            raise ManagementError('invalid_model_or_settings', '模型、连接或参数无效。启用的任务必须填写模型 ID，请检查数值范围与连接名称。') from None
        return effective

    def get(self, identifier):
        with self.lock:
            data, version = self._json(self._directory(identifier)/'config.json')
            no_secrets(data)
            # Display existing configurations without requiring an enabled model.
            effective = deep_merge(self.account(), {k:v for k,v in data.items() if k in GROUP_FIELDS})
            return dict(id=identifier, config=data, revision=version,
                        effective={key:effective[key] for key in ['models', 'model_clients', 'participation', 'memory', 'profile', 'search', 'vision', 'image_generation']})

    def save(self, identifier, data, expected, *, create=False):
        with self.lock:
            path = self._directory(identifier)/'config.json'
            if create:
                if path.exists():
                    raise ManagementError('instance_exists', '实例已经存在。', 409)
            elif revision(self._read(path)) != expected:
                raise ManagementError('revision_conflict', '配置已被其他窗口修改，请重新读取后再保存。', 409)
            if not create and self._json(path)[0].get('chat')!=data.get('chat'):
                raise ManagementError('chat_identity_immutable','会话 ID 已绑定此实例；更换会话请新建实例。',409)
            self._validate(identifier, data)
            self._write(path, (json.dumps(data, ensure_ascii=False, indent=2)+'\n').encode())
            return dict(**self.get(identifier), activation='restart_required')

    def request_instance(self, identifier, action, expected):
        from wxbridge.app.instance_control import REQUEST_FILE
        if action not in {'start','stop','apply'}:
            raise ManagementError('invalid_instance_action','不支持的实例操作。')
        with self.lock:
            item=self.get(identifier)
            if item['revision']!=expected:
                raise ManagementError('revision_conflict','实例配置已变化，请刷新后操作。',409)
            config=deepcopy(item['config'])
            if action!='apply':
                config['enabled']=action=='start'
            path=self._directory(identifier)/'config.json'
            previous=self._read(path)
            saved=self.save(identifier,config,expected)
            record=dict(id=uuid.uuid4().hex,action=action,revision=saved['revision'],created_at=time.time())
            try:
                self._write(self._directory(identifier)/REQUEST_FILE,json.dumps(record).encode())
            except Exception:
                self._write(path,previous)
                raise
            return dict(request_id=record['id'],revision=saved['revision'],enabled=config.get('enabled',True))

    def prompt(self, identifier):
        with self.lock:
            item = self.get(identifier)
            directory = self._directory(identifier)
            filename = item['config'].get('system_prompt_file', 'system.md')
            path = self._safe(directory/filename)
            if not path.resolve().is_relative_to(directory.resolve()):
                raise ManagementError('invalid_prompt_path', '此提示词位于实例目录外，请先修改文件配置。', 403)
            exists = path.is_file()
            raw = self._read(path) if exists else b''
            try:
                text = raw.decode('utf-8-sig').replace('\r\n','\n').replace('\r','\n') if exists else '你正在微信里和朋友交流，请自然、简短地回应当前消息。\n'
            except UnicodeError:
                raise ManagementError('invalid_prompt_encoding', '提示词必须为 UTF-8 文本。') from None
            return dict(text=text, revision=revision(raw) if exists else 'missing',
                        config_revision=item['revision'], file=str(path.relative_to(directory)),
                        configured='system_prompt_file' in item['config'], history=self.history(identifier))

    def history(self, identifier):
        path = self._safe(self._directory(identifier)/'.web-history')
        result = []
        for file in path.glob('*.json') if path.exists() else []:
            data, _ = self._json(file)
            result.append(dict(id=file.stem, created_at=data['created_at'], characters=len(data['text'])))
        return sorted(result, key=lambda item:item['created_at'], reverse=True)[:20]

    def save_prompt(self, identifier, text, expected, config_expected):
        if not isinstance(text, str) or not text.strip() or len(text.encode()) > 64*1024 or '\0' in text:
            raise ManagementError('invalid_prompt', '提示词不能为空，最大64 KB，且不能包含空字符。')
        with self.lock:
            current = self.prompt(identifier)
            if current['revision'] != expected or current['config_revision'] != config_expected:
                raise ManagementError('revision_conflict', '提示词或实例配置已变化，请重新读取后再保存。', 409)
            directory = self._directory(identifier)
            binding = None
            if not current['configured']:
                item = self.get(identifier)
                binding = dict(item['config'], system_prompt_file=current['file'])
                self._validate(identifier, binding)
            data = dict(text=current['text'], created_at=time.time())
            self._write(directory/'.web-history'/(uuid.uuid4().hex+'.json'), json.dumps(data, ensure_ascii=False).encode())
            path = directory/current['file']
            self._write(path, text.replace('\r\n', '\n').encode('utf-8'))
            if binding is not None:
                try:
                    self.save(identifier, binding, current['config_revision'])
                except Exception:
                    if current['revision']=='missing':
                        path.unlink(missing_ok=True)
                    else:
                        self._write(path,current['text'].encode('utf-8'))
                    raise
            return dict(**self.prompt(identifier), activation='restart_required' if binding is not None else 'next_request')

    def restore_prompt(self, identifier, history_id, expected, config_expected):
        if not re.fullmatch('[a-f0-9]{32}', history_id):
            raise ManagementError('invalid_history', '无效的历史版本。')
        with self.lock:
            data, _ = self._json(self._directory(identifier)/'.web-history'/(history_id+'.json'))
            return self.save_prompt(identifier, data['text'], expected, config_expected)

    def validate_start(self):
        try:
            cfg = load_config(self.config_file)
            if not any(entry['enabled'] for entry in cfg['instances']):
                raise ValueError('no_enabled_instances')
            return cfg
        except (ValueError, TypeError, KeyError):
            raise ManagementError('start_config_invalid', '启动前校验失败：至少启用一个实例，并为启用的任务配置有效模型。', 409) from None

    def config_hash(self):
        try:
            from wxbridge.app.build_info import runtime_identity
            return runtime_identity(load_config(self.config_file))['config_hash']
        except (ValueError, OSError, TypeError, KeyError):
            return None
