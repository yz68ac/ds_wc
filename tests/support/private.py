"""Shared synthetic private fixtures; contains no executable tests."""
from tests.support.configuration import replace_config, update_config, snapshot
import tests.support.pipeline as pipeline


class PrivateReplyFixture(pipeline.PipelineFixture):
    def make_bot(self):
        self.cfg = replace_config(self.cfg, 'chat', 'wxid_friend', owner=self)
        self.cfg = replace_config(self.cfg, 'models.participation.model', '', owner=self)
        self.store.resolve_chat.return_value = ('wxid_friend', '合成私聊')
        pipeline.PipelineFixture.make_bot(self)
