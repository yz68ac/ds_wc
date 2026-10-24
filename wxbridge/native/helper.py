"""Standalone privileged helper. Install root-owned and run with Python -I.

It imports no application/configuration code, exposes no TCP port, reads no chat DB,
and accepts only bounded text jobs from the configured local account.
"""
from __future__ import annotations

import hashlib
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
RID=re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}\Z')
CID=re.compile(r'(?:filehelper|wxid_[A-Za-z0-9_-]{1,120}|[A-Za-z0-9_-]{1,128}@chatroom)\Z')


def validate(value,allowed):
    if not isinstance(value,dict) or set(value)!={'op','request_id','chat_id','text'} or value['op']!='send_text':
        raise ValueError('native_invalid_request')
    if not isinstance(value['request_id'],str) or not RID.fullmatch(value['request_id']):
        raise ValueError('native_invalid_request')
    if not isinstance(value['chat_id'],str) or not CID.fullmatch(value['chat_id']):
        raise ValueError('native_invalid_request')
    if value['chat_id'] not in allowed:raise ValueError('native_chat_not_allowed')
    if not isinstance(value['text'],str) or not value['text'].strip() or '\0' in value['text']:
        raise ValueError('native_invalid_request')
    try:length=len(value['text'].encode('utf-8'))
    except UnicodeError:raise ValueError('native_invalid_request') from None
    if length>4096:raise ValueError('native_invalid_request')
    return length


def failure(code,submitted=False):
    return dict(ok=False,ui=dict(backend='native',error=code,operation={'submitted':submitted}))


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
    if set(value)!={'socket','uid','allowed_chat_ids','assets','worker_timeout_seconds'}:
        raise ValueError('native_helper_config')
    if type(value['uid']) is not int or value['uid']<=0:raise ValueError('native_helper_config')
    if not isinstance(value['allowed_chat_ids'],list) or not 0<len(value['allowed_chat_ids'])<=16:
        raise ValueError('native_helper_config')
    if any(not isinstance(x,str) or not CID.fullmatch(x) for x in value['allowed_chat_ids']):
        raise ValueError('native_helper_config')
    if type(value['worker_timeout_seconds']) is not int or not 5<=value['worker_timeout_seconds']<=15:
        raise ValueError('native_helper_config')
    assets=trusted(value['assets'])
    for name in ('agent.js','profile.json'):trusted(assets/name)
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


def worker(config,value):
    length=validate(value,config['allowed_chat_ids'])
    import frida  # Available only in the root-owned helper venv.
    assets=Path(config['assets'])
    profile=json.loads((assets/'profile.json').read_text())
    try:pid=find_client(config['uid'],profile['sha256'])
    except ValueError as exc:
        return failure('native_profile_mismatch' if str(exc)=='native_profile_mismatch' else 'native_wrong_client')
    session=None;script=None;armed=False;observed={};agent_error=False
    result=failure('native_worker_failed')
    try:
        session=frida.get_local_device().attach(pid)
        binding=dict(pid=pid,chat_id=value['chat_id'],text=value['text'],byte_length=length)
        source='const PROFILE='+json.dumps(profile)+';\nconst CONFIG='+json.dumps(binding,ensure_ascii=True)+';\n'+(assets/'agent.js').read_text()
        script=session.create_script(source)
        def on_message(message,data):
            nonlocal agent_error
            if message.get('type')=='error':agent_error=True
        script.on('message',on_message);script.load()
        if agent_error:return failure('native_agent_error')
        found=script.exports_sync.discover()
        if not found.get('viewFound'):return failure('native_no_unique_context')
        armed=True  # Even an RPC failure can occur after the source was enqueued.
        response=script.exports_sync.arm()
        armed=bool(response.get('accepted'))
        if not armed:return failure('native_schedule_failed')
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            observed=script.exports_sync.status()
            if observed.get('stage') not in ('observing','armed','checking','calling'):break
            time.sleep(.03)
        if observed.get('stage')=='returned_unconfirmed' and observed.get('calls')==1:
            result=dict(ok=True,ui=dict(backend='native',operation={'submitted':True},
                chars=len(value['text']),bytes=length,scanned_mib=found['scannedMiB'],main_thread=pid))
        else:
            codes={'rejected_context':'native_context_changed','wrong_thread':'native_wrong_thread',
                   'error':'native_construct_failed'}
            code=codes.get(observed.get('stage'),'native_callback_timeout')
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
    cache={}
    try:
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
                    value=recv(connection);validate(value,config['allowed_chat_ids'])
                    digest=hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=True).encode()).hexdigest()
                    old=cache.get(value['request_id'])
                    if old:
                        result=old[1] if old[0]==digest else failure('native_request_conflict')
                    elif len(cache)>=4096:result=failure('native_worker_busy')
                    else:
                        result=run_child(path,value,config['worker_timeout_seconds'])
                        cache[value['request_id']]=(digest,result)
                    reply(connection,result)
                except (OSError,ValueError,UnicodeError):
                    try:reply(connection,failure('native_invalid_request'))
                    except OSError:pass
    finally:
        stream.close()
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
