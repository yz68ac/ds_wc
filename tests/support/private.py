"""Shared synthetic private fixtures; contains no executable tests."""
import tests.support.pipeline as pipeline


class PrivateReplyFixture(pipeline.PipelineFixture):
    def make_bot(self):
        self.cfg['target']['chat'] = 'wxid_friend'
        self.cfg['models']['participation']['model'] = ''
        self.store.resolve_chat.return_value = ('wxid_friend', '合成私聊')
        pipeline.PipelineFixture.make_bot(self)
