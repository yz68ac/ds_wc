"""Bounded, safe runtime snapshots and log projections shared by runtime and management."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import stat
import time

from wxbridge.control import BOT, SESSION, RECOVERY, request as control_request

UNITS = (BOT, SESSION, RECOVERY)
EVENTS = ('接收判定', '开始生成', '生成结束', '生成失败', '生成未启动', '废弃生成', '发送意图', '发送确认',
          '发送操作', '提交前失败，安全等待重试', '搜索提示待发送', '模拟完成', '记忆更新', '记忆延后',
          '记忆初始化失败', '跳过记忆背景', '主动接话判断', '主动接话跳过', '停止监听')


def mapping(value):
    return value if isinstance(value, dict) else {}


def numeric(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 10**18


def number(value):
    return f'{value:,.0f}' if numeric(value) else '-'


def code(value):
    if (not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_./:-]{1,96}', value)
            or re.search(r'(?:sk-|gh[pousr]_|github_pat_|Bearer)', value, re.I)):
        return '-' if not value else '未识别代码'
    return value


def error(value):
    return code(':'.join(value.split(':')[:2])) if isinstance(value, str) and value else '无'


def read_bytes(path, limit, *, tail=False):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('invalid')
        if info.st_size > limit:
            if not tail:
                raise ValueError('large')
            stream.seek(info.st_size-limit)
            stream.readline(limit)  # 不把截断的半行当成完整事件。
        return stream.read(limit+1)


def read_json(path):
    try:
        value = json.loads(read_bytes(path, 2*1024*1024))
        if not isinstance(value, dict):
            raise ValueError('invalid')
        return value, ''
    except FileNotFoundError:
        return {}, 'missing'
    except PermissionError:
        return {}, 'denied'
    except (ValueError, UnicodeError, RecursionError) as exc:
        return {}, 'large' if str(exc) == 'large' else 'invalid'
    except OSError:
        return {}, 'unavailable'


def read_services():
    try:
        values = control_request('status')['components']
        services = {name: dict(Id=name,ActiveState=values[name]['state'],
                    SubState=values[name].get('substate',''),MainPID=str(values[name]['pid']))
                    for name in UNITS if name in values}
        return services, '' if services else 'unavailable'
    except (OSError, ValueError, KeyError, TypeError):
        return {}, 'unavailable'


def safe_events(path, *, instance=None):
    """只投影已知事件和字段；未知日志、正文、凭据和终端控制字符不透传。"""
    try:
        lines = read_bytes(path, 65536, tail=True).decode('utf-8', errors='replace').splitlines()
    except FileNotFoundError:
        return [], 'missing'
    except PermissionError:
        return [], 'denied'
    except (OSError, ValueError):
        return [], 'unavailable'
    result = []
    for line in lines[-300:]:
        match = re.match(r'^(\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (INFO|WARNING|ERROR|DEBUG) (.*)$', line)
        if not match:
            continue
        stamp, level, message = match.groups()
        if instance is not None and not re.search(r'\binstance='+re.escape(instance)+r'(?:\s|$)', message):
            continue
        name = next((name for name in EVENTS if message == name or message.startswith(name+' ')
                     or message.startswith(name+'，')), None)
        if name is None:
            continue
        fields = re.findall(r'\b(instance|task|version|chunks|images|questions|references|part|local_id|revision|attempt|messages|kind|mode|reason|category|error|retryable)=([A-Za-z0-9_.:-]+)', message)
        parts = [f'{key}={code(value)}' for key, value in fields]
        if 'metadata=' in message:
            try:
                metadata = mapping(json.loads(message.split('metadata=', 1)[1]))
                for key in ('local_id', 'reason', 'error'):
                    if key in metadata:
                        parts.append(f'{key}={number(metadata[key]) if key == "local_id" else error(metadata[key])}')
            except (ValueError, RecursionError):
                pass
        result.append(f'{stamp} {level:<7} {name} '+ ' '.join(parts))
    return list(reversed(result[-100:])), ''


class StatusReader:
    def __init__(self, cfg):
        self.cfg = cfg
        self.directory = Path(cfg.paths.state_file).parent
        self.services, self.events, self.service_error, self.log_error = {}, [], '', ''
        self.last_details = float('-inf')

    def read(self, *, force=False):
        live, failure = read_json(self.directory/'live-status.json')
        boot, _ = read_json(self.directory/'boot-status.json')
        tick = time.monotonic()
        if force or tick-self.last_details >= 5:
            self.services, self.service_error = read_services()
            if getattr(self.cfg, 'id', ''):
                self.events, self.log_error = safe_events(self.cfg.account.log_file, instance=self.cfg.id)
            else:
                self.events, self.log_error = safe_events(self.cfg.account.log_file)
            self.last_details = tick
        return dict(live=live, boot=boot, file_error=failure, services=self.services,
                    service_error=self.service_error, events=self.events, log_error=self.log_error,
                    collected_at=time.time())


def read_account_status(cfg):
    path = Path(cfg.paths.state_file).parent/'account-status.json'
    if not path.exists():
        return dict(phase='not_started', instances=[e.id for e in cfg.instances.values() if e.enabled])
    return json.loads(read_bytes(path, 256*1024))
