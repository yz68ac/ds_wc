"""Bounded index of confirmed pictures retained for conversation context."""
from copy import deepcopy


class ImageContextJournal:
    def remember_image(self, local_id, image, now, lifetime):
        identity = self.get('identity')
        entries = self.context_images(now)
        entries = [e for e in entries if e['local_id'] != local_id]
        entries.append(dict(local_id=local_id, identity=identity, image=deepcopy(image), expires=now+lifetime))
        # Bound both metadata count and retained image bytes per instance.
        while len(entries) > 16 or sum(e['image']['bytes'] for e in entries) > 64*1024*1024:
            entries.pop(0)
        self.set('context_images', entries)

    def context_images(self, now):
        identity = self.get('identity')
        return [e for e in self.get('context_images', [])
                if e['identity'] == identity and e['expires'] > now]

    def context_image(self, source, now):
        identity = self.get('identity')
        if (source.get('sender') != identity['account']
                or source.get('chat_id', identity['chat']) != identity['chat']):return None
        for entry in self.context_images(now):
            if (entry['local_id'] == source.get('local_id')
                    and entry['image']['md5'] == source.get('image_md5')):
                return deepcopy(entry['image'])
        return None

    def retained_image_files(self, now, *, enabled):
        entries = self.context_images(now) if enabled else []
        self.set('context_images', entries)
        return {entry['image']['file'] for entry in entries}
