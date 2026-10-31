"""HTTP administration only; no WeChat process, model client or database is started."""
from contextlib import asynccontextmanager
import asyncio
import hashlib
import json
from pathlib import Path
import secrets
import threading
import time

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from wxbridge_web.repository import ConfigRepository, ManagementError
from wxbridge_web.services import BOT, Operations, ServiceManager
from wxbridge_web.status import overview
from wxbridge_web.models import ModelCatalog


class LoginBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    password: str = Field(min_length=1,max_length=1024)


class ConfigBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    config: dict
    revision: str = Field(max_length=80)


class NewBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(min_length=1,max_length=48)
    config: dict


class PromptBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    text: str = Field(min_length=1,max_length=65536)
    revision: str = Field(max_length=80)
    config_revision: str = Field(max_length=80)


class RestoreBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: str = Field(max_length=80)
    config_revision: str = Field(max_length=80)


class InstanceActionBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: str = Field(min_length=64,max_length=64)


class Sessions:
    def __init__(self, password):
        if not password or len(password) < 12:
            raise ValueError('management_password_must_have_at_least_12_characters')
        self.salt = secrets.token_bytes(16)
        self.digest = self.hash(password)
        self.lock, self.values, self.failures = threading.RLock(), {}, {}

    def hash(self, password):
        return hashlib.pbkdf2_hmac('sha256',password.encode(),self.salt,120000)

    def login(self, password, peer):
        with self.lock:
            now = time.monotonic()
            failures = [stamp for stamp in self.failures.get(peer,[]) if now-stamp<60]
            self.failures = {key:value for key,value in self.failures.items() if value and now-value[-1]<60}
            if len(failures)>=5:
                raise ManagementError('login_rate_limit','尝试次数过多，请一分钟后重试。',429)
            if not secrets.compare_digest(self.hash(password),self.digest):
                self.failures[peer] = [*failures,now]
                raise ManagementError('invalid_password','管理口令不正确。',401)
            self.failures.pop(peer,None)
            self.values = {key:value for key,value in self.values.items() if value['expires']>now}
            if len(self.values)>=32:
                raise ManagementError('session_limit','登录会话过多，请稍后重试。',429)
            token = secrets.token_urlsafe(32)
            self.values[token] = dict(csrf=secrets.token_urlsafe(24), expires=now+8*3600)
            return token

    def get(self, token):
        with self.lock:
            value = self.values.get(token)
            if value and value['expires']>time.monotonic():
                return dict(value)
            self.values.pop(token,None)
            return None

    def logout(self, token):
        with self.lock:
            self.values.pop(token,None)


def create_app(root, password, *, services=None, demo=False, static_dir=None, allowed_hosts=None, secure_cookie=False, catalog_file=None):
    repository = ConfigRepository(root)
    services = services or ServiceManager()
    operations = Operations(services,repository)
    sessions = Sessions(password)
    catalog=ModelCatalog(repository,path=catalog_file,demo=demo)

    @asynccontextmanager
    async def lifespan(app):
        yield
        operations.close()
        catalog.close()

    app = FastAPI(title='WXBridge Console', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.repository, app.state.operations = repository, operations
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts or ['localhost','127.0.0.1','[::1]'])

    @app.exception_handler(ManagementError)
    async def management_error(request, exc):
        return JSONResponse(dict(error=exc.code,message=exc.message),status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        # Pydantic's default error body echoes rejected inputs, including passwords.
        return JSONResponse(dict(error='invalid_request',message='输入格式无效，请检查各字段。'),status_code=422)

    @app.middleware('http')
    async def access(request, call_next):
        if request.url.path.startswith('/api/'):
            if request.method in {'POST','PUT','DELETE','PATCH'}:
                origin = request.headers.get('origin')
                if origin and origin!=str(request.base_url).rstrip('/'):
                    return JSONResponse(dict(error='origin_denied',message='请求来源不匹配。'),status_code=403)
                if request.headers.get('sec-fetch-site')=='cross-site':
                    return JSONResponse(dict(error='origin_denied',message='拒绝跨站操作。'),status_code=403)
                try:
                    if int(request.headers.get('content-length','0'))>256*1024:
                        return JSONResponse(dict(error='body_too_large',message='请求超过大小上限。'),status_code=413)
                except ValueError:
                    return JSONResponse(dict(error='invalid_length'),status_code=400)
            session = sessions.get(request.cookies.get('wxbridge_session'))
            request.state.session = session
            public = request.url.path in {'/api/session','/api/login'}
            if not public and not session:
                return JSONResponse(dict(error='authentication_required',message='请先登录管理台。'),status_code=401)
            if request.method in {'POST','PUT','DELETE','PATCH'} and request.url.path!='/api/login':
                if not session or not secrets.compare_digest(request.headers.get('x-csrf-token',''),session['csrf']):
                    return JSONResponse(dict(error='csrf_denied',message='会话校验失败，请刷新页面。'),status_code=403)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        return response

    @app.get('/api/session')
    def session(request: Request):
        value = request.state.session
        return dict(authenticated=bool(value),csrf=value['csrf'] if value else None,demo=demo)

    @app.post('/api/login')
    def login(body: LoginBody, request: Request):
        token = sessions.login(body.password,request.client.host if request.client else 'local')
        response = JSONResponse(dict(authenticated=True,csrf=sessions.get(token)['csrf'],demo=demo))
        response.set_cookie('wxbridge_session',token,httponly=True,samesite='strict',secure=secure_cookie,max_age=8*3600)
        return response

    @app.delete('/api/session')
    def logout(request: Request):
        sessions.logout(request.cookies.get('wxbridge_session'))
        response = JSONResponse(dict(authenticated=False))
        response.delete_cookie('wxbridge_session')
        return response

    @app.get('/api/overview')
    def get_overview():
        return overview(repository,services,operations,demo=demo)

    @app.get('/api/events')
    async def events(request: Request):
        async def updates():
            while not await request.is_disconnected():
                if not sessions.get(request.cookies.get('wxbridge_session')):
                    yield 'event: expired\ndata: {}\n\n'
                    return
                snapshot = await run_in_threadpool(overview,repository,services,operations,demo=demo)
                yield 'event: snapshot\ndata: '+json.dumps(snapshot,ensure_ascii=False,allow_nan=False)+'\n\n'
                await asyncio.sleep(2)
        return StreamingResponse(updates(),media_type='text/event-stream',headers={'X-Accel-Buffering':'no'})

    @app.get('/api/instances')
    def instances():
        return repository.list()

    @app.post('/api/instances',status_code=201)
    def create_instance(body: NewBody):
        return repository.save(body.id,body.config,None,create=True)

    @app.get('/api/instances/{identifier}')
    def instance(identifier: str):
        return repository.get(identifier)

    @app.put('/api/instances/{identifier}')
    def save_instance(identifier: str, body: ConfigBody):
        return repository.save(identifier,body.config,body.revision)

    @app.post('/api/instances/{identifier}/actions/{action}',status_code=202)
    def instance_action(identifier: str, action: str, body: InstanceActionBody):
        snapshot=overview(repository,services,operations,demo=demo)
        running=snapshot['services'][BOT]['state']=='active'
        if running and not snapshot['capabilities']['instance_control']:
            raise ManagementError('runtime_upgrade_required','当前机器人未就绪或版本不支持实例控制，请更新后再操作。',409)
        if any(item['state']=='running' for item in operations.snapshot()):
            raise ManagementError('operation_busy','账号服务正在启停，请等待完成。',409)
        result=repository.request_instance(identifier,action,body.revision)
        return dict(**result,state='requested' if running else 'waiting_service',
                    message='实例操作已提交，等待运行层确认。' if running else '配置已保存；账号服务当前未运行，下次启动时生效。')

    @app.get('/api/models')
    def models():
        return catalog.read()

    @app.post('/api/models/refresh',status_code=202)
    def refresh_models():
        if demo:
            return dict(state='demo',message='演示使用合成模型目录，没有访问真实接口。')
        return catalog.refresh()

    @app.get('/api/instances/{identifier}/prompt')
    def prompt(identifier: str):
        return repository.prompt(identifier)

    @app.put('/api/instances/{identifier}/prompt')
    def save_prompt(identifier: str, body: PromptBody):
        return repository.save_prompt(identifier,body.text,body.revision,body.config_revision)

    @app.post('/api/instances/{identifier}/prompt/restore/{history_id}')
    def restore_prompt(identifier: str, history_id: str, body: RestoreBody):
        return repository.restore_prompt(identifier,history_id,body.revision,body.config_revision)

    @app.post('/api/services/{action}',status_code=202)
    def service_action(action: str):
        return operations.submit(action)

    static = Path(static_dir) if static_dir else Path(__file__).resolve().parents[1]/'webui/dist'
    if (static/'assets').is_dir():
        app.mount('/assets',StaticFiles(directory=static/'assets'),name='assets')

    @app.get('/')
    def index():
        if not (static/'index.html').is_file():
            return JSONResponse(dict(error='frontend_not_built',message='请先在 src/webui 运行 npm run build。'),status_code=503)
        return FileResponse(static/'index.html')

    return app
