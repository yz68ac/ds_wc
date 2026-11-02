"""Fresh immutable snapshots and explicit replacement for synthetic fixtures."""
from dataclasses import fields, is_dataclass, replace
from wxbridge.config import AppConfig, ConfigSource, FrozenMap, InstanceConfig, Section, UNSET
from wxbridge.config.codec import instance_from_document, effective_document, section_from_document


def snapshot(document):
    """Construct synthetic settings without touching real files or provider credentials."""
    return instance_from_document(document)


def application(instance):
    account=replace(instance.account,queue_file=instance.paths.queue_file,state_file=instance.paths.state_file,
                    memory_directory=instance.paths.memory_directory)
    instance=replace(instance,account=account,id=instance.id or 'synthetic',chat=instance.chat or 'synthetic@chatroom')
    return AppConfig(account=account,instances=FrozenMap(((instance.id,instance),)))


def _replace(value, path, replacement):
    name, *tail = path.split('.')
    if name == 'instances' and isinstance(value, InstanceConfig):
        if replacement == [] and not tail:return value
        value=AppConfig(account=value.account)
    if isinstance(value,FrozenMap):
        key=tuple(value)[int(name)] if name.isdigit() else name
        child=_replace(value[key],'.'.join(tail[1:] if tail and tail[0]=='config' else tail),replacement) if tail else replacement
        return FrozenMap(tuple((name,child if name==key else item) for name,item in value.items()))
    if name == 'config' and isinstance(value,InstanceConfig):
        return _replace(value,'.'.join(tail),replacement)
    if not hasattr(value,name):
        if isinstance(value,Section):
            return replace(value,extensions=FrozenMap(tuple({**dict(value.extensions),name:replacement}.items())))
        raise AttributeError(name)
    current = getattr(value, name)
    updated = _replace(current, '.'.join(tail), replacement) if tail else replacement
    if name == 'instances' and isinstance(updated, list):
        rows=[]
        for row in updated:
            item=row['config']
            if isinstance(item, dict):item=snapshot(item)
            item=replace(item, id=row['id'], chat=row.get('chat',item.chat), enabled=row['enabled'],
                         metadata=replace(item.metadata, revision=row.get('config_revision','')))
            rows.append((item.id,item))
        updated=FrozenMap(tuple(rows))
    if isinstance(current,Section) and isinstance(updated,dict):
        updated=section_from_document(type(current),updated)
    result=replace(value, **{name:updated})
    if isinstance(value,AppConfig) and name=='account':
        result=replace(result,instances=FrozenMap(tuple((key,replace(item,account=updated)) for key,item in value.instances.items())))
    return result


def replace_config(config, path, value, *, owner=None):
    for before,after in (('memory.directory','paths.memory_directory'),('queue.file','paths.queue_file'),
                         ('state_file','paths.state_file')):
        if path==before:path=after
    if path.split('.')[0] in {'db_dir','keys_file','decrypted_dir','log_file','wechat_process',
                              'source','poll_interval_seconds','provider_env','ui','native_api'}:
        path='account.'+path
    if path in {'queue','memory','target'} and isinstance(value,dict):
        key,destination={'queue':('file','paths.queue_file'),'memory':('directory','paths.memory_directory'),
                         'target':('chat','chat')}[path]
        if key in value:
            config=replace_config(config,destination,value[key],owner=owner)
            value={name:item for name,item in value.items() if name!=key}
    result=_replace(config,path,value)
    if owner is not None:
        # Older pipeline fixtures wired several controllers to one settings
        # object. Rewire those fixture owners to the newly constructed snapshot.
        seen=set()
        def rewire(obj, depth=0):
            if id(obj) in seen or depth>3 or not hasattr(obj,'__dict__'):
                return
            seen.add(id(obj))
            for name,current in tuple(vars(obj).items()):
                if current is config:
                    setattr(obj,name,result)
                elif type(current).__module__.startswith('wxbridge.app') and not is_dataclass(current):
                    rewire(current,depth+1)
                elif isinstance(config,InstanceConfig) and current is config.participation:
                    setattr(obj,name,result.participation)
        rewire(owner)
    return result


def update_config(config, path='', values=None, *, owner=None, **changes):
    for name,value in {**(values or {}),**changes}.items():
        config=replace_config(config,'.'.join(p for p in (path,name) if p),value,owner=owner)
    return config


def merge_config(base, overrides):
    from wxbridge.config_merge import deep_merge
    if isinstance(base,InstanceConfig):
        return snapshot(deep_merge(effective_document(base),overrides))
    return deep_merge(base,overrides)


def validate_models(config):
    from wxbridge.config.validation import validate_models as validate
    return validate(effective_document(config) if isinstance(config,InstanceConfig) else config)


def validate_image_model(config, *, provider_config):
    from wxbridge.config.validation import validate_image_model as validate
    return validate(effective_document(config) if isinstance(config,InstanceConfig) else config,
                    provider_config=provider_config)


def resolve_fixture(config, path, **options):
    from wxbridge.config.validation import validate_document
    document=effective_document(config) if isinstance(config,InstanceConfig) else config
    return snapshot(validate_document(document,path,**options))
