"""Model dropdowns use the provider's validated discovery catalog, never made-up IDs."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
import threading
import time

from provider import ModelError, create_client, load_config
from provider.catalog import load_catalog, matching_catalog_models
from provider.config import env_path
from provider.router import ModelRouter
from wxbridge_web.repository import ManagementError


class ModelCatalog:
    def __init__(self,repository,*,path=None,demo=False):
        self.repository,self.path,self.demo=repository,Path(path) if path else None,demo
        self.lock,self.refreshing,self.result=threading.RLock(),False,None
        self.executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='web-model-list')

    def _paths(self):
        source=env_path(self.repository.account().get('provider_env'))
        target=self.path or source.with_name('models.json')
        return source,target

    def read(self):
        source,target=self._paths()
        config=load_config(source,environ={} if self.demo else None)
        catalog=target if target.exists() else source.with_name('models.json')
        try:
            entries=load_catalog(catalog,config)
            matched=matching_catalog_models(config,entries)
            error=''
        except ModelError:
            entries,matched,error={}, {}, 'invalid_model_catalog'
        providers=[]
        for name in config.connections:
            entry=entries.get(name)
            models=[dict(id=model.id,name=model.display_name or model.id) for model in matched.get(name,())]
            valid=name in matched
            providers.append(dict(id=name,protocol=config.protocols[name],configured=bool(config.api_key(name)),
                image_protocol='gemini' if config.options[name].get('image_protocol') == 'gemini' else 'openai'
                    if config.protocols[name] in ('openai', 'openai_compatible', 'openai_responses') else None,
                image_configured=bool(config.api_key(name)) and
                    (bool(config.options[name].get('image_base_url')) if name == 'gemini' else name == 'openai'),
                models=models,fetched_at=entry.fetched_at if entry and valid else None,
                state='ready' if valid else 'stale' if entry else 'empty'))
        with self.lock:
            return dict(providers=providers,error=error,refreshing=self.refreshing,last_refresh=self.result)

    def refresh(self):
        with self.lock:
            if self.refreshing:
                raise ManagementError('model_refresh_busy','模型列表正在更新，请稍候。',409)
            self.refreshing=True
            self.executor.submit(self._refresh)
            return dict(state='refreshing')

    def _refresh(self):
        result=dict(updated={},errors={},skipped=[],finished_at=None)
        try:
            source,target=self._paths()
            config=load_config(source)
            with ExitStack() as stack:
                clients={}
                for name in config.connections:
                    if not config.api_key(name):
                        continue
                    try:
                        clients[name]=stack.enter_context(create_client(name,config=config))
                    except ModelError as exc:
                        result['errors'][name]=exc.code
                router=ModelRouter.from_env(source,models_path=target,clients=clients)
                synced=router.sync_models(timeout=20)
                result['updated']=synced.updated
                result['errors']={**synced.errors,**result['errors']}
                result['skipped']=list(synced.skipped)
        except ModelError as exc:
            result['errors']['catalog']=exc.code
        except (OSError,ValueError):
            result['errors']['catalog']='model_catalog_update_failed'
        except Exception:
            result['errors']['catalog']='model_discovery_failed'
        finally:
            with self.lock:
                result['finished_at']=time.time()
                self.result,self.refreshing=result,False

    def close(self):
        self.executor.shutdown(wait=False,cancel_futures=True)
