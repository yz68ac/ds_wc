"""Owner-loop image generation, private artifacts and late-result rejection."""
from concurrent.futures import CancelledError
from copy import deepcopy
import hashlib
import io
from pathlib import Path
import time
import warnings

from provider import ImageRequest, GeneratedImage, ModelError
from wxbridge.ai.roles import image_request
from wxbridge.contracts.image_jobs import ImageProposal, QuotedImageInput, QueuedImageJob

from wxbridge.storage.image_artifacts import MAX_BYTES, FILE_NAME, artifact_directory, write_artifact


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

    def proposal(self, values, *, triggers=()):
        if not values:
            return None
        if not self.bot.cfg.image_generation.enabled or self.bot.quiescing:
            raise ModelError('image_generation_disabled')
        if self.bot.journal.pending_count() >= self.bot.cfg.queue.max_pending:
            raise ModelError('image_queue_full')
        if (not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], ImageProposal)
                or not isinstance(values[0].prompt, str) or not 1 <= len(values[0].prompt.strip()) <= 4000):
            raise ModelError('invalid_image_tool')
        proposal = values[0]
        request = image_request(self.bot.cfg, proposal.prompt.strip())
        source_image = None
        image = proposal.reference_image
        if image is not None:
            from wxbridge.ai.composer import reference_source
            if (not isinstance(image, QuotedImageInput)
                    or type(image.trigger_index) is not int or not 0 <= image.trigger_index < len(triggers)
                    or type(image.depth) is not int or not 0 <= image.depth < 8):
                raise ModelError('invalid_image_edit_source')
            reference = triggers[image.trigger_index].get('reference')
            for _ in range(image.depth):
                reference = reference.get('reference') if isinstance(reference, dict) else None
            if (not isinstance(reference, dict) or reference.get('kind') != 'image'
                    or reference_source(reference) != image.source
                    or (reference.get('chat_id') and reference['chat_id'] != self.bot.chat_id)):
                raise ModelError('invalid_image_edit_source')
            data, metadata = checked_image(GeneratedImage(image.data, mime=image.mime))
            source_image = write_artifact(self.bot.cfg.paths.queue_file, data, metadata)
        return QueuedImageJob(request.model, request.prompt, deepcopy(request.options), source_image)

    def discard_proposal(self, proposal):
        if proposal is not None and proposal.source_image is not None:
            image = proposal.source_image
            (Path(self.bot.cfg.paths.queue_file).parent/'generated-images'/image['file']).unlink(missing_ok=True)

    def _valid(self, task, now):
        if (not task or task['phase'] == 'done' or self.bot.quiescing
                or not self.bot.cfg.image_generation.enabled
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
                image = write_artifact(self.bot.cfg.paths.queue_file, data, metadata)
                path = artifact_directory(self.bot.cfg.paths.queue_file)/image['file']
                files.append(path)
                # Validation and disk work may consume the remaining deadline.
                if not self._valid(task, time.time()):
                    raise CancelledError()
                with journal.db:
                    task.update(generation='ready', chunks=[''], images=[image])
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
                if (not self.bot.cfg.image_generation.enabled or now >= task.get('reply_expires_at', 0)
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
            if self.bot.cfg.account.ui.dry_run or self.bot.echo:
                with journal.db:
                    self._cancel(task, now)
                continue
            # Durable running marker precedes the external request. Recovery never regenerates it.
            request = QueuedImageJob.from_document(task['image_request'])
            source_image = request.source_image
            with journal.db:
                task['generation'] = 'image_running'
                journal.put(task)
            try:
                options = dict(source_image=source_image) if source_image else {}
                future = self.bot.submit_images(ImageRequest(request.model, request.prompt, request.options),
                    deadline=time.monotonic()+max(0, task['image_deadline']-time.time()), **options)
                self.jobs[task['id']] = task['version'], future
            except Exception as exc:
                with journal.db:
                    journal.image_failed(task, getattr(exc, 'code', 'image_generation_failed'), now)
        self.cleanup(now)

    def cleanup(self, now, *, force=False):
        if not force and now-self.cleaned_at < 30:
            return
        root = Path(self.bot.cfg.paths.queue_file).parent/'generated-images'
        if not root.exists() or root.is_symlink():
            return
        active = {image['file'] for t in self.bot.journal.image_tasks() for image in t.get('images', [])}
        active.update(t['image_request']['source_image']['file'] for t in self.bot.journal.image_tasks()
                      if (t.get('image_request') or {}).get('source_image'))
        with self.bot.journal.db:
            active.update(self.bot.journal.retained_image_files(now, enabled=(
                self.bot.cfg.vision.enabled and (self.bot.cfg.vision.context_images_enabled
                                                or self.bot.cfg.image_generation.enabled))))
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
        self.cleanup(time.time(), force=True)
