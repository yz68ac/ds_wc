"""Model-independent records; callers own their receive cursor."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


@dataclass(frozen=True)
class Message:
    chat_id: str
    local_id: int
    server_id: str
    sender_id: str
    sender_name: str
    type: int
    time: int
    text: str = field(repr=False)
    mentioned_users: tuple[str, ...] | None = None
    reference: dict | None = field(default=None, repr=False)
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)
    raw_type: int = 0
    message_kind: str = ''
    pat: dict | None = field(default=None, repr=False)
    image_md5: str = ''
    image_cache_key: str = ''

    @classmethod
    def from_mapping(cls, chat_id, value):
        mentions=value.get('mentioned_users')
        return cls(chat_id,value['local_id'],str(value.get('server_id') or ''),value.get('sender',''),
                   value.get('sender_name',''),value.get('type',0),value.get('time',0),value.get('text',''),
                   tuple(mentions) if mentions is not None else None,deepcopy(value.get('reference')),deepcopy(value),
                   value.get('raw_type',value.get('type',0)),value.get('message_kind',''),
                   deepcopy(value.get('pat')),value.get('image_md5',''),value.get('image_cache_key',''))


@dataclass(frozen=True)
class MessageBatch:
    messages: tuple[Message, ...]
    cursor: int


class DeliveryState(str, Enum):
    NOT_SUBMITTED = 'not_submitted'
    SUBMITTED = 'submitted'
    CONFIRMED = 'confirmed'
    UNKNOWN = 'unknown'


@dataclass(frozen=True)
class SendReceipt:
    request_id: str
    chat_id: str
    state: DeliveryState
    ack_id: int | None = None
    error: str = ''
    metadata: dict = field(default_factory=dict, repr=False, compare=False)


@dataclass(frozen=True)
class Image:
    data: bytes = field(repr=False)
    info: dict
