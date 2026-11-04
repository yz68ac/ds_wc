"""Web-editable business settings, derived from the runtime's typed defaults."""
from dataclasses import fields
import json
import math
import os

from wxbridge.config.codec import INSTANCE_SECTIONS, account_document, json_value
from wxbridge.config.models import InstanceConfig, Section
from .repository import ManagementError, no_secrets, revision


# Labels describe product behavior; defaults and constraints belong to config models.
LABELS = {
    'target.bot_name':'机器人称呼', 'target.cooldown_seconds':'回复冷却 / 秒',
    'target.max_message_age_seconds':'可处理消息最大年龄 / 秒',
    'target.context_recent_messages':'上下文最近消息数', 'target.context_max_age_seconds':'上下文时间范围 / 秒',
    'target.context_bot_messages':'上下文机器人消息数', 'target.context_max_chars':'上下文字符预算',
    'target.context_budget_strategy':'上下文裁剪策略', 'target.max_reply_chars':'每条发送消息字符上限',
    'reply_style.wechat_emoticons_enabled':'微信表情文字', 'reply_style.omit_trailing_period':'省略回复末尾句号',
    'participation.enabled':'自动回复', 'participation.debounce_seconds':'合并消息等待 / 秒',
    'participation.max_wait_seconds':'最长合并等待 / 秒', 'participation.max_batch_messages':'每批消息上限',
    'participation.address_names':'被称呼时识别的名字', 'participation.pat_enabled':'响应拍一拍',
    'memory.enabled':'会话记忆', 'memory.context_window_seconds':'记忆上下文窗口 / 秒',
    'memory.min_interval_seconds':'记忆整理最小间隔 / 秒', 'memory.prompt_max_chars':'注入回复的记忆字符上限',
    'memory.learning.enabled':'关系学习', 'memory.learning.passive_enabled':'被动学习',
    'memory.learning.window_seconds':'学习窗口 / 秒', 'memory.learning.min_new_messages':'学习所需新消息数',
    'memory.learning.min_senders':'学习所需发言人数', 'memory.learning.min_interval_seconds':'学习最小间隔 / 秒',
    'memory.avatar.enabled':'头像分类', 'memory.avatar.max_per_batch':'每批头像数',
    'memory.avatar.max_bytes':'头像大小上限 / 字节', 'memory.avatar.retry_interval_seconds':'头像重试间隔 / 秒',
    'profile.enabled':'群资料分析', 'profile.initial_history_days':'初次分析历史天数',
    'profile.initial_max_messages':'初次分析消息上限', 'profile.batch_size':'分析批次大小',
    'profile.min_new_messages':'触发分析所需新消息数', 'profile.min_interval_seconds':'分析最小间隔 / 秒',
    'profile.summary_max_chars':'群资料摘要字符上限', 'profile.analysis_max_chars':'分析输入字符上限',
    'search.enabled':'联网搜索', 'search.backend':'搜索方式', 'search.connection':'搜索连接',
    'search.timeout_seconds':'单次搜索超时 / 秒', 'search.total_timeout_seconds':'搜索总超时 / 秒',
    'tools.max_rounds':'工具调用轮数上限', 'tools.total_timeout_seconds':'回复与工具总超时 / 秒',
    'search.max_requests':'搜索请求数上限',
    'search.max_results':'搜索结果数上限', 'search.feedback_enabled':'发送搜索进度提示',
    'search.feedback_phrases':'搜索进度提示语',
    'vision.enabled':'图片理解', 'vision.context_images_enabled':'自动将图片加入上下文',
    'vision.detail':'图片细节级别', 'vision.max_images':'每次图片上限',
    'vision.max_image_bytes':'单张图片大小上限 / 字节', 'vision.cache_wait_seconds':'等待媒体缓存 / 秒',
    'vision.download_enabled':'主动下载图片', 'vision.download_backend':'图片下载方式',
    'vision.download_timeout_seconds':'图片下载超时 / 秒',
    'queue.max_pending':'实例待处理任务上限', 'queue.max_pending_per_sender':'每位发言者任务上限',
    'queue.generation_lookahead':'预生成任务窗口', 'queue.intake_max_pages':'每轮消息扫描页数',
    'queue.generation_retries':'生成重试次数', 'queue.generation_retry_delay_seconds':'生成重试间隔 / 秒',
    'queue.completed_retention':'保留已完成任务数',
}
GROUPS = (
    ('reply','回复与上下文',('target','reply_style'),'控制回复节奏、发送分片与上下文。超过每条消息字符上限时分条发送，保留完整回复。微信表情文字开关不限制 Unicode emoji，也不改写代码、引用和链接。recent 优先保留最近内容，strict 严格检查预算。'),
    ('participation','自动回复与参与',('participation',),'自动回复开关同时控制群聊与私聊。开启后，私聊直接回复，群聊由参与模型判断；关闭后不再接收新的自动回复消息。最长合并等待不能小于合并消息等待。'),
    ('memory','记忆与学习',('memory',),'记忆整理、被动关系学习和头像分类。相关模型在基本配置中选择。'),
    ('profile','群资料',('profile',),'群资料分析的采样与频率。触发消息数不能大于批次大小。'),
    ('tools','工具调用',('tools',),'限制单次回复与工具调用的总时长和调用轮数。'),
    ('search','联网搜索',('search',),'搜索总超时只限制本次回复中的搜索；连接密钥使用 Provider 文件配置。'),
    ('vision','图片理解',('vision',),'自动加入开启后，回复模型可读取上下文中的聊天图片和机器人已发送图片。需要支持图片输入的回复模型；引用图片优先，共用每次图片上限。主动下载仍由下载开关控制。'),
    ('queue','队列与重试',('queue',),'限制当前实例的任务积压和重试。每位发言者上限不能大于实例上限。'),
)


def _fields(section, prefix):
    for field in fields(section):
        if field.name=='extensions':continue
        value=getattr(section,field.name)
        path=prefix+'.'+field.name
        if isinstance(value,Section):
            yield from _fields(value,path)
        elif path in LABELS:
            item=dict(path=path,label=LABELS[path],default=json_value(value),
                      type='boolean' if type(value) is bool else 'number' if type(value) in (int,float)
                      else 'list' if isinstance(value,tuple) else 'text')
            if item['type']=='number':item['integer']=type(value) is int
            if 'range' in field.metadata:
                low,high=field.metadata['range']
                item.update(min=math.ceil(low) if type(value) is int else low,max=high if math.isfinite(high) else None)
            if 'choices' in field.metadata:item.update(type='select',choices=list(field.metadata['choices']))
            yield item


def instance_schema():
    template=InstanceConfig()
    groups=[]
    for key,label,roots,description in GROUPS:
        groups.append(dict(id=key,label=label,description=description,
                           fields=[item for root in roots for item in _fields(getattr(template,root),root)]))
    model_groups=[]
    for role,label in (('reply','回复模型'),('participation','参与模型'),('memory','记忆模型'),('avatar','头像模型'),('group_profile','群资料模型')):
        options=getattr(template.models,role).options
        entries=[]
        for key,title,low,high in (('max_tokens','最大输出 token',1,128000),('timeout_seconds','请求超时 / 秒',1,300),
                                    ('temperature','采样温度',0,2),('thinking_budget_tokens','思考 token 预算',0,None)):
            value=getattr(options,key)
            entries.append(dict(path=f'models.{role}.options.{key}',label=title,type='number',min=low,max=high,
                                integer=key in ('max_tokens','thinking_budget_tokens'),
                                default=value if type(value) in (int,float) else None,optional=True))
        for key,title,choices in (('thinking','思考开关',['disabled','enabled']),
                                  ('reasoning_effort','思考档位',None),('thinking_mode','思考模式',None)):
            value=getattr(options,key)
            entries.append(dict(path=f'models.{role}.options.{key}',label=title,type='select' if choices else 'text',
                                choices=choices,default=value if isinstance(value,str) else None,optional=True))
        model_groups.append(dict(id=role,label=label,description='仅设置当前模型和协议支持的选项。可清除覆盖，沿用默认值。',fields=entries))
    return dict(groups=groups,defaults={name:json_value(getattr(template,name)) for name in INSTANCE_SECTIONS},
                model_groups=model_groups,account_fields=ACCOUNT_FIELDS)


ACCOUNT_FIELDS = [
    dict(path='poll_interval_seconds',label='消息轮询间隔 / 秒',type='number',min=0.1,step=0.1),
    dict(path='account.send_interval_seconds',label='账号发送间隔 / 秒',type='number',min=0,max=60,step=0.1),
    dict(path='ui.send_confirm_timeout_seconds',label='发送确认超时 / 秒',type='number',min=1,integer=True),
    dict(path='ui.dry_run',label='演练模式（不实际发送）',type='boolean'),
]


def _value(document,path):
    value=document
    for name in path.split('.'):value=value[name]
    return value


def account_settings(repository):
    with repository.lock:
        raw=repository._read(repository.config_file)
        document=account_document(repository.account())
        return dict(revision=revision(raw),values={f['path']:_value(document,f['path']) for f in ACCOUNT_FIELDS},
                    editable=os.access(repository.config_file.parent,os.W_OK),
                    runtime=dict(source=document['source'],backend=document['ui']['backend'],
                                 provider_env=document['provider_env'],database_bound=bool(document['db_dir'])))


def save_account_settings(repository,values,expected):
    allowed={f['path']:f for f in ACCOUNT_FIELDS}
    if not isinstance(values,dict) or not values or set(values)-allowed.keys():
        raise ManagementError('invalid_account_settings','只允许修改列出的账号业务参数。')
    no_secrets(values)
    with repository.lock:
        if not os.access(repository.config_file.parent,os.W_OK):
            raise ManagementError('account_settings_read_only','当前账号配置目录只读，请在服务器上修改。',403)
        raw=repository._read(repository.config_file)
        if revision(raw)!=expected:raise ManagementError('revision_conflict','账号配置已变化，请重新读取。',409)
        document=json.loads(raw.decode('utf-8-sig'))
        for path,value in values.items():
            field=allowed[path]
            if field['type']=='boolean':valid=type(value) is bool
            else:
                valid=type(value) in (int,float) and math.isfinite(value) and value>=field['min']
                if field.get('max') is not None:valid=valid and value<=field['max']
                if field.get('integer'):valid=valid and type(value) is int
            if not valid:raise ManagementError('invalid_account_settings','账号参数类型或数值范围无效。')
            parts=path.split('.');target=document
            for part in parts[:-1]:target=target.setdefault(part,{})
            target[parts[-1]]=value
        from wxbridge.config.loader import _validated_account
        try:_validated_account(document,repository.config_file,environment=False)
        except (ValueError,TypeError,KeyError):raise ManagementError('invalid_account_settings','账号配置校验失败。') from None
        try:repository._write(repository.config_file,(json.dumps(document,ensure_ascii=False,indent=2)+'\n').encode())
        except OSError:raise ManagementError('account_settings_write_failed','保存失败，请检查账号配置目录权限。',403) from None
        return dict(**account_settings(repository),activation='account_apply_required')
