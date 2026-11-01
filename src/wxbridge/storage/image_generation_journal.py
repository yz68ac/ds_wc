"""Image task records share the existing journal and its owning main loop."""
from copy import deepcopy


class ImageGenerationJournal:
    def enqueue_image_generation(self, parent, request, now):
        """Called inside generated()'s transaction, before any chargeable image call."""
        if parent.get('image_task_id'):
            return parent['image_task_id']
        identifier = self._next_task_id()
        timeout = request['options'].get('timeout_seconds', 180)
        task = dict(id=identifier, version=1, phase='queued', trigger_kind='image_generation',
                    sender=parent['sender'], triggers=[], trigger_ids=[], generation='image_pending',
                    chunks=[], next_chunk=0, intent=None, blocked_reason='',
                    deadline=now, accepted_at=now, closed_at=now,
                    image_deadline=now+timeout, reply_expires_at=now+timeout+90,
                    parent_task_id=parent['id'], parent_version=parent['version'],
                    image_identity=self.get('identity'), image_request=deepcopy(request))
        parent['image_task_id'] = identifier
        self.put(task)
        return identifier

    def image_tasks(self):
        return [t for t in self.tasks() if t.get('trigger_kind') == 'image_generation']

    def image_failed(self, task, code, now):
        task.update(generation='ready', generation_failed=True, image_error=code,
                    chunks=['这次没画出来，稍后再试吧'], next_chunk=0)
        task.pop('image_request', None)
        self.set('last_image_generation', dict(task_id=task['id'], state='failed', error=code, at=now))
        self.put(task)
