"""Owner-loop image generation, private artifacts and late-result rejection."""
from concurrent.futures import CancelledError
from copy import deepcopy
import hashlib
import io
import os
from pathlib import Path
import re
import stat
import time
import uuid
import warnings

from provider import ImageRequest, ModelError
from wxbridge.ai.roles import image_request

MAX_BYTES = 8 * 1024 * 1024
FILE_NAME = re.compile(r'[a-f0-9]{32}\.(?:png|jpg)\Z')


def artifact_directory(cfg):
    root = Path(cfg['queue']['file']).parent/'generated-images'
    if root.is_symlink():
        raise ModelError('image_artifact_unavailable')
    root.mkdir(mode=0o700, exist_ok=True)
    info = root.stat()
    if not stat.S_ISDIR(info.st_mode) or (os.name == 'posix' and
            (info.st_uid != os.geteuid() or info.st_mode & 0o077)):
        raise ModelError('image_artifact_unavailable')
    return root


def artifact_path(cfg, image):
    name = image.get('file')
    if not isinstance(name, str) or not FILE_NAME.fullmatch(name):
        raise ModelError('image_artifact_unavailable')
    path = artifact_directory(cfg)/name
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)), 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or not 1 <= info.st_size <= MAX_BYTES
                    or (os.name == 'posix' and (info.st_uid != os.geteuid() or info.st_mode & 0o077))):
                raise ModelError('image_artifact_unavailable')
            data = stream.read(MAX_BYTES+1)
        if len(data) != image['bytes'] or hashlib.sha256(data).hexdigest() != image['sha256']:
            raise ModelError('image_artifact_changed')
    except (OSError, KeyError):
        raise ModelError('image_artifact_unavailable') from None
    return path


def checked_image(image):
    """Decode a bounded raster; normalize WebP for the existing PNG/JPEG sender."""
    data = image.data
    if not isinstance(data, bytes) or not 1 <= len(data) <= MAX_BYTES:
        raise ModelError('image_size_limit' if data else 'image_bytes_required')
    try:
        from PIL import Image
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as decoded:
                width, height = decoded.size
                fmt = decoded.format
                if fmt not in ('PNG', 'JPEG', 'WEBP'):
                    raise ModelError('unsupported_image_format')
                mime = {'PNG': 'image/png', 'JPEG': 'image/jpeg', 'WEBP': 'image/webp'}[fmt]
                if image.mime and image.mime != mime:
                    raise ModelError('image_mime_mismatch')
                if not 1 <= width <= 8192 or not 1 <= height <= 8192 or width*height > 32*1024*1024:
                    raise ModelError('image_dimension_limit')
                decoded.verify()
            with Image.open(io.BytesIO(data)) as decoded:
                decoded.load()
                if fmt == 'WEBP':
                    buffer = io.BytesIO()
                    decoded.save(buffer, format='PNG')
                    data, mime = buffer.getvalue(), 'image/png'
        if len(data) > MAX_BYTES:
            raise ModelError('image_size_limit')
        # NativeSender applies this same structural contract before IPC.
        from wechat_linux.media.images import validate_image
        validate_image(data, MAX_BYTES)
        return data, dict(mime=mime, width=width, height=height, bytes=len(data),
                          sha256=hashlib.sha256(data).hexdigest(), md5=hashlib.md5(data, usedforsecurity=False).hexdigest())
    except ModelError:
        raise
    except ImportError:
        raise ModelError('image_decoder_not_installed') from None
    except Exception:
        raise ModelError('invalid_image_data') from None


class ImageGenerationController:
    def __init__(self, bot):
        self.bot, self.jobs = bot, {}
        self.cleaned_at = 0

    def proposal(self, values):
        if not values:
            return None
        if not self.bot.cfg['image_generation']['enabled'] or self.bot.quiescing:
            raise ModelError('image_generation_disabled')
        if self.bot.journal.pending_count() >= self.bot.cfg['queue']['max_pending']:
            raise ModelError('image_queue_full')
        if (not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict)
                or set(values[0]) != {'prompt'} or not isinstance(values[0]['prompt'], str)
                or not 1 <= len(values[0]['prompt'].strip()) <= 4000):
            raise ModelError('invalid_image_tool')
        request = image_request(self.bot.cfg, values[0]['prompt'].strip())
        return dict(model=request.model, prompt=request.prompt, options=deepcopy(request.options))

    def _valid(self, task, now):
        if (not task or task['phase'] == 'done' or self.bot.quiescing
                or not self.bot.cfg['image_generation']['enabled']
                or self.bot.journal.get('identity') != task.get('image_identity')
                or task['image_identity'] != dict(account=self.bot.store.self_wxid, chat=self.bot.chat_id)):
            return False
        parent = self.bot.journal.task(task['parent_task_id'])
        if parent and (parent['version'] != task['parent_version'] or parent.get('cancelled')):
            return False
        return now < task['image_deadline']

    def _cancel(self, task, now):
        task.update(phase='done', generation='cancelled', cancelled='image_result_discarded',
                    chunks=[], completed_at=now)
        task.pop('image_request', None)
        self.bot.journal.put(task)

    def step(self):
        now = time.time()
        journal = self.bot.journal
        for identifier, (version, future) in list(self.jobs.items()):
            task = journal.task(identifier)
            if not self._valid(task, now) or task['version'] != version:
                getattr(future, 'cancel_request', future.cancel)()
                with journal.db:
                    if task and task['phase'] != 'done':
                        self._cancel(task, now)
                del self.jobs[identifier]
                continue
            if not future.done():
                continue
            del self.jobs[identifier]
            self.bot.metrics.record_models(getattr(future, 'model_metrics', []))
            files = []
            try:
                result = future.result()
                # One generated reply is one image; reject an unexpected batch before writing.
                if len(result.images) != 1:
                    raise ModelError('unexpected_image_count')
                data, metadata = checked_image(result.images[0])
                name = uuid.uuid4().hex+('.png' if metadata['mime'] == 'image/png' else '.jpg')
                path = artifact_directory(self.bot.cfg)/name
                with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as stream:
                    files.append(path)
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                # Validation and disk work may consume the remaining deadline.
                if not self._valid(task, time.time()):
                    raise CancelledError()
                with journal.db:
                    task.update(generation='ready', chunks=[''], images=[dict(file=name, **metadata)])
                    task.pop('image_request', None)
                    journal.set('last_image_generation', dict(task_id=identifier, state='ready', images=1, at=now,
                                                             usage=result.usage))
                    journal.put(task)
                files.clear()
                self.bot.log.info('生图完成 task=%s images=1', identifier)
            except Exception as exc:
                code = getattr(exc, 'code', 'image_generation_failed')
                with journal.db:
                    if self._valid(task, time.time()):
                        journal.image_failed(task, code, now)
                    else:
                        self._cancel(task, now)
                self.bot.log.warning('生图未完成 task=%s category=%s', identifier, code)
            finally:
                for path in files:
                    path.unlink(missing_ok=True)
        if self.bot.quiescing:
            return
        for task in journal.image_tasks():
            if task['generation'] == 'ready' and not task['intent']:
                parent = journal.task(task['parent_task_id'])
                if (not self.bot.cfg['image_generation']['enabled'] or now >= task.get('reply_expires_at', 0)
                        or task.get('image_identity') != journal.get('identity')
                        or parent and (parent['version'] != task['parent_version'] or parent.get('cancelled'))):
                    with journal.db:
                        self._cancel(task, now)
                continue
            if task['generation'] != 'image_pending':
                continue
            if not self._valid(task, now):
                with journal.db:
                    self._cancel(task, now)
                continue
            if self.bot.cfg['ui']['dry_run'] or self.bot.echo:
                with journal.db:
                    self._cancel(task, now)
                continue
            # Durable running marker precedes the external request. Recovery never regenerates it.
            request = deepcopy(task['image_request'])
            with journal.db:
                task['generation'] = 'image_running'
                journal.put(task)
            try:
                future = self.bot.submit_images(ImageRequest(**request),
                    deadline=time.monotonic()+max(0, task['image_deadline']-time.time()))
                self.jobs[task['id']] = task['version'], future
            except Exception as exc:
                with journal.db:
                    journal.image_failed(task, getattr(exc, 'code', 'image_generation_failed'), now)
        self.cleanup(now)

    def cleanup(self, now):
        if now-self.cleaned_at < 30:
            return
        root = Path(self.bot.cfg['queue']['file']).parent/'generated-images'
        if not root.exists() or root.is_symlink():
            return
        active = {image['file'] for t in self.bot.journal.image_tasks() for image in t.get('images', [])}
        if self.bot.ui_job:
            sending = self.bot.journal.task(self.bot.ui_job[0])
            active.update(image['file'] for image in (sending or {}).get('images', []))
        for path in root.iterdir():
            if FILE_NAME.fullmatch(path.name) and path.name not in active and not path.is_symlink():
                path.unlink(missing_ok=True)
        self.cleaned_at = now

    def close(self):
        for _, future in self.jobs.values():
            getattr(future, 'cancel_request', future.cancel)()
        self.jobs.clear()
        with self.bot.journal.db:
            for task in self.bot.journal.image_tasks():
                if not task['intent']:
                    self._cancel(task, time.time())
        self.cleanup(float('inf'))
