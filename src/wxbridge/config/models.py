"""Immutable application settings; defaults are defined here, never loaded from JSON."""
from __future__ import annotations

from collections.abc import Mapping, Iterator
from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
from typing import ClassVar, Generic, TypeVar, get_type_hints, get_args, get_origin
import types
import math
from functools import lru_cache

T = TypeVar('T')


class ConfigError(ValueError):
    def __init__(self, code: str, path: str = ''):
        self.code, self.path = code, path
        super().__init__(code + (':' + path if path else ''))


class Missing(Enum):
    VALUE = 'unset'


UNSET = Missing.VALUE


@dataclass(frozen=True, slots=True)
class FrozenMap(Mapping[str, T], Generic[T]):
    entries: tuple[tuple[str, T], ...] = ()

    def __post_init__(self):
        if any(not isinstance(key, str) for key, _ in self.entries):
            raise ConfigError('invalid_config_key')
        object.__setattr__(self, 'entries', tuple((key, freeze(value)) for key, value in self.entries))
        if len({key for key, _ in self.entries}) != len(self.entries):
            raise ConfigError('duplicate_config_key')

    def __getitem__(self, key: str) -> T:
        for name, value in self.entries:
            if name == key:
                return value
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return (key for key, _ in self.entries)

    def __len__(self):
        return len(self.entries)


def freeze(value):
    if isinstance(value, FrozenMap):
        return value
    if isinstance(value, Mapping):
        return FrozenMap(tuple(value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool, Missing)):
        return value
    if is_dataclass(value) and type(value).__module__ == __name__ and getattr(type(value), '__dataclass_params__').frozen:
        return value
    raise ConfigError('invalid_config_value')


def _matches(value, annotation):
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is types.UnionType:
        return any(_matches(value, item) for item in args)
    if origin is tuple:
        return isinstance(value, tuple) and (not args or all(_matches(item, args[0]) for item in value))
    if origin is FrozenMap:
        return isinstance(value, FrozenMap) and (not args or all(_matches(item, args[0]) for item in value.values()))
    if annotation is float:
        return type(value) in (int, float) and math.isfinite(value)
    if annotation in (int, bool):
        return type(value) is annotation
    return annotation is object or isinstance(value, annotation)


@lru_cache(maxsize=None)
def _hints(cls):
    return get_type_hints(cls)


@dataclass(frozen=True, slots=True, kw_only=True)
class Section:
    extensions: FrozenMap[object] = field(default_factory=FrozenMap, repr=False)
    strict_fields: ClassVar[bool] = False

    def __post_init__(self):
        hints = _hints(type(self))
        for item in fields(self):
            value = freeze(getattr(self, item.name))
            if not _matches(value, hints[item.name]):
                raise ConfigError('invalid_config_type', type(self).__name__ + '.' + item.name)
            object.__setattr__(self, item.name, value)

    def validate(self, path=''):
        if self.strict_fields and self.extensions:
            raise ConfigError('invalid_config_fields', path)
        for item in fields(self):
            value = getattr(self, item.name)
            name = '.'.join(part for part in (path, item.name) if part)
            if isinstance(value, Section):
                value.validate(name)
            if value is UNSET or value is None:
                continue
            rule = item.metadata
            if 'range' in rule:
                low, high = rule['range']
                if not low <= value <= high or rule.get('positive') and value == 0:
                    raise ConfigError(rule.get('error', 'invalid_config_range'), name)
            if 'choices' in rule and value not in rule['choices']:
                raise ConfigError(rule.get('error', 'invalid_config_choice'), name)
            if rule.get('nonempty') and (not isinstance(value, str) or not value.strip()):
                raise ConfigError(rule.get('error', 'invalid_config_text'), name)
            if 'strings' in rule:
                minimum, maximum, max_items, controls = rule['strings']
                if (max_items and len(value)>max_items or minimum and not value and not rule.get('allow_empty')
                        or any(not minimum <= len(text.strip()) <= maximum
                               or controls and any(ord(char)<32 for char in text) for text in value)):
                    raise ConfigError(rule.get('error', 'invalid_config_strings'), name)
        return self


@dataclass(frozen=True, slots=True)
class ConfigSource:
    path: str = ''
    revision: str = ''
    origins: FrozenMap[str] = field(default_factory=FrozenMap)


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    queue_file: str = '/run/wxbridge-trial/queue.sqlite3'
    state_file: str = '/run/wxbridge-trial/state.json'
    memory_directory: str = 'memory'


@dataclass(frozen=True, slots=True)
class RuntimePolicy:
    reply_to_backlog: bool = False
    prepared: bool = False
    desired_fingerprint: str = ''


@dataclass(frozen=True, slots=True, kw_only=True)
class HelperTransportConfig(Section):
    enabled: bool = False
    socket: str = '/run/wxbridge-trial/native-helper.sock'
    allowed_chat_ids: tuple[str, ...] = ('filehelper',)
    max_text_bytes: int = 4096


@dataclass(frozen=True, slots=True, kw_only=True)
class SubmissionAPIConfig(Section):
    enabled: bool = False
    socket: str = '/run/wxbridge-trial/native-api.sock'
    max_pending: int = 16
    max_requests: int = 1024


@dataclass(frozen=True, slots=True, kw_only=True)
class UIConfig(Section):
    backend: str = field(default='auto', metadata={'choices': ('auto', 'atspi', 'xdotool', 'native_service'), 'error': 'ui.backend must be auto/atspi/xdotool/native_service'})
    app_names: tuple[str, ...] = ('wechat', 'WeChat', '微信')
    chat_list_names: tuple[str, ...] = ('Chats', '聊天', '会话')
    messages_list_names: tuple[str, ...] = ('Messages', '消息')
    window_class: str = 'wechat'
    native_executable: str = '/opt/wechat/wechat'
    window_names: tuple[str, ...] = ('微信', 'WeChat')
    search_shortcut: str = 'ctrl+f'
    paste_shortcut: str = 'ctrl+v'
    select_all_shortcut: str = 'ctrl+a'
    send_key: str = 'Return'
    input_method: str = 'auto'
    verify_chat_selected: bool = True
    post_send_wait: float = 0.8
    send_confirm_timeout_seconds: int = field(default=10, metadata={'range': (0, float('inf')), 'positive': True, 'error': 'ui.send_confirm_timeout_seconds 必须大于 0'})
    chat_open_wait: int = 1
    dry_run: bool = True


@dataclass(frozen=True, slots=True, kw_only=True)
class TargetConfig(Section):
    bot_name: str = '小c'
    cooldown_seconds: int = 8
    max_message_age_seconds: int = field(default=90, metadata={'range': (1, 3600), 'error': 'invalid_config_range'})
    context_recent_messages: int = field(default=30, metadata={'range': (1, 200), 'error': 'invalid_config_range'})
    context_max_age_seconds: int = field(default=1800, metadata={'range': (1, 86400), 'error': 'invalid_config_range'})
    context_bot_messages: int = field(default=3, metadata={'range': (0, 10), 'error': 'invalid_config_range'})
    context_max_chars: int = field(default=15000, metadata={'range': (1, 100000), 'error': 'invalid_config_range'})
    context_budget_strategy: str = field(default='recent', metadata={'choices': ('strict', 'recent'), 'error': 'target.context_budget_strategy must be strict/recent'})
    max_reply_chars: int = field(default=1800, metadata={'range': (1, 100000), 'error': 'invalid_config_range'})


@dataclass(frozen=True, slots=True, kw_only=True)
class QueueConfig(Section):
    max_pending: int = field(default=32, metadata={'range': (1, 1000), 'error': 'invalid_config_range'})
    max_pending_per_sender: int = field(default=8, metadata={'range': (1, 1000), 'error': 'invalid_config_range'})
    generation_lookahead: int = field(default=4, metadata={'range': (1, 32), 'error': 'invalid_config_range'})
    intake_max_pages: int = field(default=4, metadata={'range': (1, 20), 'error': 'invalid_config_range'})
    generation_retries: int = field(default=2, metadata={'range': (0, 3), 'error': 'invalid_config_range'})
    generation_retry_delay_seconds: int = field(default=3, metadata={'range': (1, 60), 'error': 'invalid_config_range'})
    completed_retention: int = field(default=200, metadata={'range': (0, 10000), 'error': 'invalid_config_range'})


@dataclass(frozen=True, slots=True, kw_only=True)
class ParticipationConfig(Section):
    strict_fields: ClassVar[bool] = True
    enabled: bool = False
    debounce_seconds: int = field(default=2, metadata={'range': (0, 60), 'error': 'invalid_config_range'})
    max_wait_seconds: int = field(default=5, metadata={'range': (0.1, 60), 'error': 'invalid_config_range'})
    max_batch_messages: int = field(default=8, metadata={'range': (1, 32), 'error': 'invalid_config_range'})
    address_names: tuple[str, ...] = field(default=(), metadata={'strings': (1, 32, 10, True), 'allow_empty': True})
    pat_enabled: bool = True


@dataclass(frozen=True, slots=True, kw_only=True)
class ReplyStyleConfig(Section):
    wechat_emoticons_enabled: bool = False
    omit_trailing_period: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchConfig(Section):
    enabled: bool = True
    backend: str = field(default='external', metadata={'choices': ('external', 'native', 'auto'), 'error': 'invalid_search_backend'})
    timeout_seconds: int = field(default=15, metadata={'range': (1, 60), 'error': 'invalid_config_range'})
    total_timeout_seconds: int = field(default=120, metadata={'range': (15, 300), 'error': 'invalid_config_range'})
    max_requests: int = field(default=3, metadata={'range': (1, 5), 'error': 'invalid_config_range'})
    max_results: int = field(default=5, metadata={'range': (1, 10), 'error': 'invalid_config_range'})
    feedback_enabled: bool = False
    feedback_phrases: tuple[str, ...] = field(default=('我上网搜搜，核实一下。', '我去看看。'), metadata={'strings': (1, 80, 0, False)})
    connection: str = 'deepseek'


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolConfig(Section):
    strict_fields: ClassVar[bool] = True
    max_rounds: int = field(default=2, metadata={'range': (1, 3), 'error': 'invalid_config_range'})
    total_timeout_seconds: int = field(default=120, metadata={'range': (1, 300), 'error': 'invalid_config_range'})


@dataclass(frozen=True, slots=True, kw_only=True)
class VisionConfig(Section):
    enabled: bool = True
    context_images_enabled: bool = False
    detail: str = field(default='original', metadata={'choices': ('original', 'high', 'low', 'auto')})
    max_images: int = field(default=4, metadata={'range': (1, 4), 'error': 'invalid_config_range'})
    max_image_bytes: int = field(default=8388608, metadata={'range': (1, 8388608), 'error': 'invalid_config_range'})
    cache_wait_seconds: int = field(default=3, metadata={'range': (0, 10), 'error': 'invalid_config_range'})
    download_enabled: bool = False
    download_backend: str = field(default='desktop', metadata={'choices': ('desktop', 'native')})
    download_timeout_seconds: int = field(default=20, metadata={'range': (5, 30), 'error': 'invalid_config_range'})


@dataclass(frozen=True, slots=True, kw_only=True)
class ProfileConfig(Section):
    strict_fields: ClassVar[bool] = True
    enabled: bool = False
    initial_history_days: int = field(default=7, metadata={'range': (1, 30), 'error': 'invalid_profile_option'})
    initial_max_messages: int = field(default=300, metadata={'range': (1, 1000), 'error': 'invalid_profile_option'})
    batch_size: int = field(default=60, metadata={'range': (1, 100), 'error': 'invalid_profile_option'})
    min_new_messages: int = field(default=50, metadata={'range': (1, 1000), 'error': 'invalid_profile_option'})
    min_interval_seconds: int = field(default=1800, metadata={'range': (60, 86400), 'error': 'invalid_profile_option'})
    summary_max_chars: int = field(default=1200, metadata={'range': (100, 4000), 'error': 'invalid_profile_option'})
    analysis_max_chars: int = field(default=24000, metadata={'range': (2000, 100000), 'error': 'invalid_profile_option'})


@dataclass(frozen=True, slots=True, kw_only=True)
class ImageGenerationConfig(Section):
    strict_fields: ClassVar[bool] = True
    enabled: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelClients(Section):
    strict_fields: ClassVar[bool] = True
    reply: str = 'deepseek'
    image: str = 'openai'
    background: str = 'deepseek'


@dataclass(frozen=True, slots=True, kw_only=True)
class LearningConfig(Section):
    enabled: bool = True
    passive_enabled: bool = True
    window_seconds: int = field(default=300, metadata={'range': (60, 3600), 'error': 'invalid_config_range'})
    min_new_messages: int = field(default=10, metadata={'range': (2, 100), 'error': 'invalid_config_range'})
    min_senders: int = field(default=2, metadata={'range': (2, 20), 'error': 'invalid_config_range'})
    min_interval_seconds: int = field(default=300, metadata={'range': (300, 86400), 'error': 'invalid_config_range'})


@dataclass(frozen=True, slots=True, kw_only=True)
class AvatarConfig(Section):
    enabled: bool = True
    max_per_batch: int = field(default=2, metadata={'range': (1, 2), 'error': 'invalid_config_range'})
    max_bytes: int = field(default=524288, metadata={'range': (1024, 524288), 'error': 'invalid_config_range'})
    retry_interval_seconds: int = field(default=3600, metadata={'range': (60, 86400), 'error': 'invalid_config_range'})


@dataclass(frozen=True, slots=True, kw_only=True)
class MemoryConfig(Section):
    enabled: bool = True
    context_window_seconds: int = field(default=300, metadata={'range': (1, 86400), 'error': 'invalid_config_range'})
    system_prompt_file: str = field(default='prompts/memory.md', metadata={'nonempty': True})
    learning: LearningConfig = field(default_factory=LearningConfig)
    min_interval_seconds: int = field(default=60, metadata={'range': (60, 86400), 'error': 'invalid_config_range'})
    prompt_max_chars: int = field(default=1200, metadata={'range': (100, 1200), 'error': 'invalid_config_range'})
    avatar: AvatarConfig = field(default_factory=AvatarConfig)


@dataclass(frozen=True, slots=True, kw_only=True)
class TextOptions(Section):
    strict_fields: ClassVar[bool] = True
    max_tokens: int | None = 1200
    timeout_seconds: float = 60
    thinking: str = 'disabled'
    temperature: float | None | Missing = UNSET
    reasoning_effort: str | None | Missing = UNSET
    thinking_mode: str | None | Missing = UNSET
    thinking_budget_tokens: int | None | Missing = UNSET


@dataclass(frozen=True, slots=True, kw_only=True)
class ImageOptions(Section):
    timeout_seconds: float = 180
    n: int | None | Missing = UNSET
    size: str | None | Missing = UNSET
    quality: str | None | Missing = UNSET
    background: str | None | Missing = UNSET
    output_format: str | None | Missing = UNSET
    output_compression: int | None | Missing = UNSET
    moderation: str | None | Missing = UNSET
    safety_identifier: str | None | Missing = UNSET
    response_format: str | None | Missing = UNSET
    style: str | None | Missing = UNSET
    user: str | None | Missing = UNSET
    aspect_ratio: str | None | Missing = UNSET
    image_size: str | None | Missing = UNSET


@dataclass(frozen=True, slots=True, kw_only=True)
class TextModelConfig(Section):
    strict_fields: ClassVar[bool] = True
    model: str = ''
    options: TextOptions = field(default_factory=TextOptions)


@dataclass(frozen=True, slots=True, kw_only=True)
class ImageModelConfig(Section):
    strict_fields: ClassVar[bool] = True
    model: str = ''
    options: ImageOptions = field(default_factory=ImageOptions)


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelsConfig(Section):
    strict_fields: ClassVar[bool] = True
    image: ImageModelConfig = field(default_factory=ImageModelConfig)
    reply: TextModelConfig = field(default_factory=TextModelConfig)
    participation: TextModelConfig = field(default_factory=lambda: TextModelConfig(options=TextOptions(max_tokens=256, timeout_seconds=20)))
    memory: TextModelConfig = field(default_factory=TextModelConfig)
    avatar: TextModelConfig = field(default_factory=TextModelConfig)
    group_profile: TextModelConfig = field(default_factory=lambda: TextModelConfig(options=TextOptions(max_tokens=1800, timeout_seconds=45)))


@dataclass(frozen=True, slots=True, kw_only=True)
class AccountConfig(Section):
    db_dir: str = ''
    keys_file: str = '/run/wxbridge-trial/keys.json'
    decrypted_dir: str = '/run/wxbridge-trial/decrypted'
    state_file: str = RuntimePaths().state_file
    log_file: str = '/run/wxbridge-trial/bot.log'
    wechat_process: str = 'wechat'
    source: str = 'db'
    poll_interval_seconds: float = field(default=2, metadata={'range': (0, float('inf')), 'positive': True, 'error': 'poll_interval_seconds 必须是有限的正数'})
    provider_env: str | None = None
    queue_file: str = RuntimePaths().queue_file
    memory_directory: str = RuntimePaths().memory_directory
    send_interval_seconds: float = 1
    ui: UIConfig = field(default_factory=UIConfig)
    helper_transport: HelperTransportConfig = field(default_factory=HelperTransportConfig)
    submission_api: SubmissionAPIConfig = field(default_factory=SubmissionAPIConfig)
    metadata: ConfigSource = field(default_factory=ConfigSource)


@dataclass(frozen=True, slots=True, kw_only=True)
class InstanceConfig(Section):
    id: str = ''
    enabled: bool = True
    chat: str = ''
    account: AccountConfig = field(default_factory=AccountConfig)
    paths: RuntimePaths = field(default_factory=RuntimePaths)
    metadata: ConfigSource = field(default_factory=ConfigSource)
    runtime: RuntimePolicy = field(default_factory=RuntimePolicy)
    system_prompt_file: str = 'prompts/system.md'
    models: ModelsConfig = field(default_factory=ModelsConfig)
    target: TargetConfig = field(default_factory=TargetConfig)
    queue: QueueConfig = field(default_factory=QueueConfig)
    participation: ParticipationConfig = field(default_factory=ParticipationConfig)
    reply_style: ReplyStyleConfig = field(default_factory=ReplyStyleConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    tools: ToolConfig = field(default_factory=ToolConfig)
    vision: VisionConfig = field(default_factory=VisionConfig)
    profile: ProfileConfig = field(default_factory=ProfileConfig)
    image_generation: ImageGenerationConfig = field(default_factory=ImageGenerationConfig)
    model_clients: ModelClients = field(default_factory=ModelClients)
    memory: MemoryConfig = field(default_factory=MemoryConfig)


@dataclass(frozen=True, slots=True, kw_only=True)
class AppConfig(Section):
    account: AccountConfig = field(default_factory=AccountConfig)
    instances: FrozenMap[InstanceConfig] = field(default_factory=FrozenMap)
    metadata: ConfigSource = field(default_factory=ConfigSource)
    runtime: RuntimePolicy = field(default_factory=RuntimePolicy)

    @property
    def paths(self):
        return RuntimePaths(self.account.queue_file, self.account.state_file, self.account.memory_directory)


__all__ = [
    'ConfigError', 'Missing', 'UNSET', 'FrozenMap', 'Section', 'ConfigSource',
    'RuntimePaths', 'RuntimePolicy', 'HelperTransportConfig', 'SubmissionAPIConfig', 'UIConfig', 'TargetConfig',
    'QueueConfig', 'ParticipationConfig', 'ReplyStyleConfig', 'SearchConfig', 'ToolConfig',
    'VisionConfig', 'ProfileConfig', 'ImageGenerationConfig', 'ModelClients',
    'LearningConfig', 'AvatarConfig', 'MemoryConfig', 'TextOptions', 'ImageOptions',
    'TextModelConfig', 'ImageModelConfig', 'ModelsConfig', 'AccountConfig',
    'InstanceConfig', 'AppConfig',
]
