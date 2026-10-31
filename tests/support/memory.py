"""Shared synthetic memory fixtures; contains no executable tests."""
from tests.support.model_support import default_config
from pathlib import Path
import tempfile
import unittest
from wxbridge.ai.memory import snapshot
from wxbridge.storage.person_memory import MemoryStore
from tests.support.pipeline import PipelineFixture
from tests.support.synthetic import NOW, msg
from tests.support.paths import ROOT


IDENTITY = dict(account="wxid_bot", chat="test@chatroom")


def suggestion(label="wxid_a", source="m1", delta=0, kind="preference", mode="real", replaces=None):
    return dict(updates=[dict(member=label, memories=[dict(kind=kind, mode=mode, summary="喜欢简洁直接的答复",
        sources=[source], confidence=.8, replaces=replaces)],
        affinity=dict(delta=delta, reason="本人礼貌交流" if delta else "", sources=[source] if delta else []))])


class MemoryFixture(unittest.TestCase):
    def setUp(self):
        root = ROOT/".test-tmp"
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temp.cleanup)
        self.memory = MemoryStore(self.temp.name, IDENTITY)
        self.cfg = default_config()

        self.cfg['system_prompt_file'] = str(ROOT/'prompts/system.md')

    def snap(self, identifier=1, sender="wxid_a", name="同名", when=NOW, context=None):
        m = msg(identifier, when, "我喜欢简洁直接的回复", sender)
        m["sender_name"] = name
        return snapshot(self.cfg, context or [m], [m], IDENTITY, self.memory, when)


class MemoryPipelineFixture(PipelineFixture):
    def enable(self):
        self.bot.close()
        self.cfg["memory"].update(enabled=True, directory=str(Path(self.temp.name)/"memory"))
        self.cfg["queue"]["debounce_seconds"] = 0
        self.make_bot()

    def confirmed(self):
        self.add(1)
        self.bot.tick()
        self.generated()
        self.add(2, sender="wxid_self", text="合成回复", mention=False)
        self.bot.tick()


def observation(scope='member', member='wxid_a', expression='[捂脸]', stance='support', replaces=None, basis=None, source='m1'):
    return dict(scope=scope, member=member if scope == 'member' else None, expression=expression,
                context='自嘲出糗', meaning='缓和尴尬、无奈自嘲', sources=[source], stance=stance, replaces=replaces, basis=basis)
