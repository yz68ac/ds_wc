"""Standalone privileged helper. Install root-owned and run with Python -I.

It imports no application/configuration code, exposes no TCP port, reads no chat DB,
and accepts bounded jobs from the configured local account. Native image support
is separately enabled; old configurations start no image worker or threads.
"""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import socket
import stat
import struct
import subprocess
import sys
import time

LIMIT=32768
MAX_IMAGE_BYTES=8*1024*1024
RID=re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}\Z')
CID=re.compile(r'(?:filehelper|wxid_[A-Za-z0-9_-]{1,120}|[A-Za-z0-9_-]{1,128}@chatroom)\Z')


def validate(value,allowed):
    if not isinstance(value,dict) or value.get('op') not in ('send_text','send_image'):
        raise ValueError('native_invalid_request')
    fields=({'op','request_id','chat_id','text'} if value['op']=='send_text' else
            {'op','request_id','chat_id','image_path','image_bytes','image_sha256','image_mime'})
    if set(value)!=fields:raise ValueError('native_invalid_request')
    if not isinstance(value['request_id'],str) or not RID.fullmatch(value['request_id']):
        raise ValueError('native_invalid_request')
    if not isinstance(value['chat_id'],str) or not CID.fullmatch(value['chat_id']):
        raise ValueError('native_invalid_request')
    if value['chat_id'] not in allowed:raise ValueError('native_chat_not_allowed')
    if value['op']=='send_image':
        path=value['image_path']
        if (not isinstance(path,str) or not path.startswith('/') or '\0' in path
                or len(path.encode('utf-8'))>4096 or os.path.normpath(path)!=path
                or type(value['image_bytes']) is not int or not 1<=value['image_bytes']<=MAX_IMAGE_BYTES
                or not isinstance(value['image_sha256'],str) or not re.fullmatch('[0-9a-f]{64}',value['image_sha256'])
                or value['image_mime'] not in ('image/png','image/jpeg')):
            raise ValueError('native_invalid_request')
        return value['image_bytes']
    if not isinstance(value['text'],str) or not value['text'].strip() or '\0' in value['text']:
        raise ValueError('native_invalid_request')
    try:length=len(value['text'].encode('utf-8'))
    except UnicodeError:raise ValueError('native_invalid_request') from None
    if length>4096:raise ValueError('native_invalid_request')
    return length


def image_snapshot(value,uid):
    """Read only a regular image owned and readable by the configured client UID."""
    try:
        path=Path(value['image_path'])
        if str(path.resolve(strict=True))!=str(path):raise ValueError('native_image_unavailable')
        flags=os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0)
        with os.fdopen(os.open(path,flags),'rb') as stream:
            before=os.fstat(stream.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_uid!=uid or not before.st_mode&stat.S_IRUSR
                    or before.st_size!=value['image_bytes'] or not 1<=before.st_size<=MAX_IMAGE_BYTES):
                raise ValueError('native_image_unavailable')
            data=stream.read(MAX_IMAGE_BYTES+1)
            after=os.fstat(stream.fileno())
        identity=lambda info:(info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)
        if identity(before)!=identity(after) or identity(after)!=identity(path.stat(follow_symlinks=False)):
            raise ValueError('native_image_changed')
        if len(data)!=value['image_bytes'] or hashlib.sha256(data).hexdigest()!=value['image_sha256']:
            raise ValueError('native_image_changed')
        signature=b'\x89PNG\r\n\x1a\n' if value['image_mime']=='image/png' else b'\xff\xd8\xff'
        if not data.startswith(signature):raise ValueError('native_image_invalid')
        return identity(after)
    except OSError:
        raise ValueError('native_image_unavailable') from None


def failure(code,submitted=False):
    return dict(ok=False,ui=dict(backend='native_service',error=code,operation={'submitted':submitted}))


def recv(stream):
    def read(count):
        body=bytearray()
        while len(body)<count:
            part=stream.recv(count-len(body))
            if not part:raise ValueError('native_invalid_request')
            body.extend(part)
        return bytes(body)
    length=struct.unpack('!I',read(4))[0]
    if not 0<length<=LIMIT:raise ValueError('native_invalid_request')
    return json.loads(read(length).decode('utf-8'))


def reply(stream,value):
    body=json.dumps(value,ensure_ascii=True,separators=(',',':')).encode()
    if len(body)>LIMIT:raise ValueError('native_invalid_request')
    stream.sendall(struct.pack('!I',len(body))+body)


def trusted(path):
    path=Path(path)
    if not path.is_absolute():raise ValueError('native_helper_config')
    for item in (path,*path.parents):
        info=item.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022:
            raise ValueError('native_helper_config')
    return path


def configuration(path):
    value=json.loads(trusted(path).read_text())
    required={'socket','uid','allowed_chat_ids','assets','worker_timeout_seconds'}
    if not isinstance(value,dict) or set(value)-{'image_download'}!=required:
        raise ValueError('native_helper_config')
    if type(value['uid']) is not int or value['uid']<=0:raise ValueError('native_helper_config')
    if not isinstance(value['allowed_chat_ids'],list) or not value['allowed_chat_ids']:
        raise ValueError('native_helper_config')
    if any(not isinstance(x,str) or not CID.fullmatch(x) for x in value['allowed_chat_ids']):
        raise ValueError('native_helper_config')
    if type(value['worker_timeout_seconds']) is not int or not 5<=value['worker_timeout_seconds']<=15:
        raise ValueError('native_helper_config')
    assets=trusted(value['assets'])
    for name in ('agent.js','profile.json'):trusted(assets/name)
    image=value.get('image_download',{'enabled':False})
    if not isinstance(image,dict) or type(image.get('enabled')) is not bool:
        raise ValueError('native_helper_config')
    if image['enabled']:
        if (set(image)!={'enabled','expected_account','account_root','timeout_seconds'}
                or not isinstance(image['expected_account'],str)
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',image['expected_account'])
                or type(image['timeout_seconds']) is not int or not 5<=image['timeout_seconds']<=30
                or not isinstance(image['account_root'],str)):
            raise ValueError('native_helper_config')
        account=Path(image['account_root'])
        if (not account.is_absolute() or str(account.resolve())!=str(account)
                or not account.is_dir() or account.stat().st_uid!=value['uid']
                or not (account.name==image['expected_account'] or account.name.startswith(image['expected_account']+'_'))
                or any(p.is_symlink() for p in (account,*account.parents))):
            raise ValueError('native_helper_config')
        for name in ('image_worker.py','image_contract.py','image_agent.js','image_adapter.c','image_profile.json'):
            trusted(assets/name)
    elif set(image)!={'enabled'}:
        raise ValueError('native_helper_config')
    if not isinstance(value['socket'],str) or not value['socket'].startswith('/run/') or len(value['socket'].encode())>100:
        raise ValueError('native_helper_config')
    return value


def find_client(uid,expected_hash):
    pids=[]
    for item in Path('/proc').iterdir():
        if not item.name.isdigit():continue
        try:
            if item.stat().st_uid==uid and (item/'exe').resolve()==Path('/opt/wechat/wechat'):
                pids.append(int(item.name))
        except OSError:pass
    if len(pids)!=1:raise ValueError('native_wrong_client')
    pid=pids[0]
    with open(f'/proc/{pid}/exe','rb') as stream:
        if hashlib.file_digest(stream,'sha256').hexdigest()!=expected_hash:
            raise ValueError('native_profile_mismatch')
    return pid


def health(config):
    profile=json.loads((Path(config['assets'])/'profile.json').read_text())
    try:
        find_client(config['uid'],profile['sha256'])
    except ValueError as exc:
        return dict(ok=False,error='native_profile_mismatch' if str(exc)=='native_profile_mismatch' else 'native_wrong_client')
    features=['send_text']+(['send_image'] if profile.get('send_image') else [])
    return dict(ok=True,result=dict(backend='native_service',client_ready=True,features=features))


def worker(config,value):
    started=time.monotonic()
    length=validate(value,config['allowed_chat_ids'])
    image=value['op']=='send_image'
    if image:
        try:image_snapshot(value,config['uid'])
        except ValueError as exc:return failure(str(exc))
    import frida  # Available only in the root-owned helper venv.
    assets=Path(config['assets'])
    profile=json.loads((assets/'profile.json').read_text())
    if profile.get('backend')!='service_v1':return failure('native_profile_mismatch')
    if image and not profile.get('send_image'):return failure('native_profile_mismatch')
    try:pid=find_client(config['uid'],profile['sha256'])
    except ValueError as exc:
        return failure('native_profile_mismatch' if str(exc)=='native_profile_mismatch' else 'native_wrong_client')
    verified=time.monotonic()
    session=None;script=None;armed=False;observed={};agent_error=False
    result=failure('native_worker_failed')
    try:
        session=frida.get_local_device().attach(pid)
        binding=dict(pid=pid,op=value['op'],chat_id=value['chat_id'],byte_length=length)
        if image:
            binding['image_path']=value['image_path']
        else:
            binding['text']=value['text']
        source='const PROFILE='+json.dumps(profile)+';\nconst CONFIG='+json.dumps(binding,ensure_ascii=True)+';\n'+(assets/'agent.js').read_text()
        script=session.create_script(source)
        def on_message(message,data):
            nonlocal agent_error
            if message.get('type')=='error':agent_error=True
        script.on('message',on_message);script.load()
        attached=time.monotonic()
        if agent_error:return failure('native_agent_error')
        if image:
            try:image_snapshot(value,config['uid'])
            except ValueError as exc:return failure(str(exc))
        armed=True  # Even an RPC failure can occur after the source was enqueued.
        response=script.exports_sync.arm()
        armed=bool(response.get('accepted'))
        if not armed:return failure('native_schedule_failed')
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            observed=script.exports_sync.status()
            if observed.get('stage') not in ('observing','armed','checking','calling','submitting_unknown'):break
            time.sleep(.03)
        if (observed.get('stage')=='returned_unconfirmed' and observed.get('calls')==1
                and observed.get('recipient_verified') is True and not observed.get('cleanup_error')
                and (not image or (observed.get('image_source_verified') is True and observed.get('message_type')==3))):
            result=dict(ok=True,ui=dict(backend='native_service',operation={'submitted':True},
                bytes=length,recipient_verified=True,main_thread=pid,
                timings_ms=dict(verify=round((verified-started)*1000,2),
                                attach=round((attached-verified)*1000,2),
                                submit=round((time.monotonic()-attached)*1000,2))))
            if image:
                result['ui'].update(message_type=3,image_sha256=value['image_sha256'],image_bytes=length,image_mime=value['image_mime'])
                try:image_snapshot(value,config['uid'])
                except ValueError as exc:result=failure(str(exc),None)
            else:result['ui']['chars']=len(value['text'])
        else:
            codes={'rejected_context':'native_context_changed','wrong_thread':'native_wrong_thread',
                   'error':'native_construct_failed'}
            code=codes.get(observed.get('stage'),'native_callback_timeout')
            if observed.get('error') in ('registry_bounds','registry_cycle','duplicate_service','services_not_found'):
                code='native_services_unavailable'
            if observed.get('cleanup_error'):code='native_cleanup_failed'
            if observed.get('error')=='image_source_fields':code='native_image_source_mismatch'
            result=failure(code,None if observed.get('calls') else False)
    except Exception:
        result=failure('native_worker_failed',None if armed else False)
    finally:
        if script:
            try:
                cancelled=script.exports_sync.cancel()
                if not result.get('ok') and armed:
                    result['ui']['operation']['submitted']=None if cancelled.get('calls') else False
            except Exception:
                if armed:result=failure('native_worker_failed',None)
        if session:
            try:session.detach()
            except Exception:
                if armed:result=failure('native_worker_failed',None)
    return result


def run_child(config_path,value,seconds):
    try:
        run=subprocess.run([sys.executable,'-I',str(Path(__file__).resolve()),'--worker',str(config_path)],
            input=json.dumps(value,ensure_ascii=True),capture_output=True,text=True,timeout=seconds)
    except subprocess.TimeoutExpired:return failure('native_helper_timeout',None)
    if run.returncode:return failure('native_worker_failed',None)
    try:
        result=json.loads(run.stdout)
        if set(result)!={'ok','ui'} or not isinstance(result['ui'],dict):raise ValueError()
        return result
    except (ValueError,TypeError):return failure('native_worker_failed',None)


def serve_locked(path,config):
    target=Path(config['socket'])
    info=target.parent.stat()
    if info.st_uid!=config['uid'] or info.st_mode&0o077:raise ValueError('native_helper_config')
    if target.is_symlink():raise ValueError('native_helper_config')
    if target.exists():
        old=target.lstat()
        if not stat.S_ISSOCK(old.st_mode) or old.st_uid!=0:raise ValueError('native_helper_config')
        target.unlink()
    stream=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
    # Opportunistic IPC replay cache only. The main SQLite journal owns durable
    # dedupe and never resubmits an unknown intent, including after helper restart.
    cache=OrderedDict()
    images=None
    try:
        if config.get('image_download',{}).get('enabled'):
            spec=importlib.util.spec_from_file_location('_trusted_wechat_image_worker',
                                                       trusted(Path(config['assets'])/'image_worker.py'))
            module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
            images=module.ImageManager(config,find_client)
        import pwd
        stream.bind(str(target));os.chown(target,0,pwd.getpwuid(config['uid']).pw_gid);target.chmod(0o660)
        inode=target.stat().st_ino;stream.listen(4)
        def stop(signum,frame):raise KeyboardInterrupt()
        signal.signal(signal.SIGTERM,stop)
        while True:
            connection,_=stream.accept()
            with connection:
                connection.settimeout(3)
                peer=struct.unpack('3i',connection.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))[1]
                if peer not in (0,config['uid']):continue
                try:
                    value=recv(connection)
                    if value=={'op':'health'}:
                        reply(connection,health(config))
                        continue
                    if isinstance(value,dict) and value.get('op') in ('download_image','image_status'):
                        reply(connection,images.handle(value) if images else
                              dict(ok=False,error='native_image_disabled',requested=False))
                        continue
                    validate(value,config['allowed_chat_ids'])
                    digest=hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=True).encode()).hexdigest()
                    old=cache.get(value['request_id'])
                    if old:
                        result=old[1] if old[0]==digest else failure('native_request_conflict')
                        cache.move_to_end(value['request_id'])
                    else:
                        result=run_child(path,value,config['worker_timeout_seconds'])
                        if len(cache)>=4096:cache.popitem(last=False)
                        cache[value['request_id']]=(digest,result)
                    reply(connection,result)
                except (OSError,ValueError,UnicodeError):
                    try:reply(connection,failure('native_invalid_request'))
                    except OSError:pass
    finally:
        stream.close()
        if images is not None:images.close()
        try:
            if target.lstat().st_ino==inode:target.unlink()
        except (FileNotFoundError,UnboundLocalError):pass


def serve(path):
    import fcntl
    config=configuration(path)
    lock=Path(config['socket']).with_suffix('.lock')
    fd=os.open(lock,os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600)
    try:
        info=os.fstat(fd)
        if info.st_uid!=0 or not stat.S_ISREG(info.st_mode) or info.st_mode&0o077:
            raise ValueError('native_helper_config')
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        serve_locked(path,config)
    finally:os.close(fd)


def main():
    try:
        if len(sys.argv)!=3 or sys.argv[1] not in ('--worker','--serve'):raise ValueError()
        config=configuration(sys.argv[2])
        if sys.argv[1]=='--worker':
            raw=sys.stdin.buffer.read(LIMIT+1)
            if len(raw)>LIMIT:raise ValueError()
            result=worker(config,json.loads(raw.decode()))
            print(json.dumps(result,ensure_ascii=True));return 0
        serve(sys.argv[2]);return 0
    except KeyboardInterrupt:return 0
    except Exception:
        # No exception messages, bodies, command arguments or tracebacks in root logs.
        if '--worker' in sys.argv:print(json.dumps(failure('native_worker_failed',None)))
        else:print('native_helper_failed',file=sys.stderr)
        return 1


if __name__=='__main__':raise SystemExit(main())
