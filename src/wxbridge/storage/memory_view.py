"""Detached read-only memory contract; consumers cannot mutate the repository."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from wxbridge.storage import expression_memory, relationships
from wxbridge.storage.member_references import safe_legacy_metadata
from wxbridge.storage.memory_rules import affinity_value, usable_entries


def _freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _plain(value):
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


@dataclass(frozen=True)
class MemoryView:
    """A consistent revision for prompt rendering and model evidence selection.

    This view stays in the owner process. Only detached, JSON-compatible model
    snapshots cross a worker boundary; MappingProxyType is intentionally not an
    IPC payload or a new persistence format.
    """

    _state: Mapping = field(repr=False)

    def __post_init__(self):
        object.__setattr__(self, '_state', _freeze(self._state))

    @classmethod
    def capture(cls, data):
        return cls(data)

    @property
    def identity(self):
        return self._state['identity']

    @property
    def revision(self):
        return self._state['revision']

    @property
    def watermark(self):
        return self._state['watermark']

    @property
    def processed_ids(self):
        return frozenset(self._state.get('processed_ids', ()))

    @property
    def members(self):
        return self._state['members']

    @property
    def learning_floor(self):
        return max(self.watermark, self._state.get('learning', {}).get('started_after_id', 0))

    def background(self, labels, now):
        result = {}
        for uid, label in labels.items():
            person = self.members.get(uid)
            if person:
                result[label] = dict(nickname=person['nickname'], affinity=affinity_value(person, now),
                    entries=_plain(usable_entries(person, now)), relationship=safe_legacy_metadata(
                        _plain(relationships.background(person, now))))
        return result

    def expression_background(self, labels, now):
        return _plain(expression_memory.background(self._state, labels, now))

    def expression_prompt(self, labels, asker, dialogue, budget, now):
        return expression_memory.prompt(self._state, labels, asker, dialogue, budget, now)

    def prompt(self, labels, asker, now, budget=1200, **options):
        from wxbridge.storage.memory_render import render_memory
        return render_memory(self, labels, asker, now, budget, **options)

    def reply_relationship(self, member, now):
        person = self.members.get(member)
        if person is None:
            return None
        relation = relationships.state(person)
        result = dict(affinity=affinity_value(person, now), familiarity=relation['familiarity'], trust=relation['trust'])
        mood = relation.get('mood')
        if mood and now < mood['expires_at']:
            intensity = 1 if now-mood['time'] >= relationships.MOOD_TTL/2 else mood['intensity']
            result['mood'] = dict(state=mood['state'], intensity=intensity)
        return result
