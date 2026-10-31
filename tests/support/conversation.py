"""Shared synthetic conversation fixtures; contains no executable tests."""
import tests.support.pipeline as pipeline


class ConversationFixture(pipeline.PipelineFixture):
    def make_bot(self):
        self.cfg['participation'].update(enabled=True, debounce_seconds=0)
        pipeline.PipelineFixture.make_bot(self)
        def make(kind, payload, **kwargs):
            job = pipeline.FakeJob(kind, payload, **kwargs)
            (self.decision_jobs if kind == 'participation' else self.jobs).append(job)
            return job
        self.bot.job_factory = make

    def ordinary(self, text, sender='wxid_friend', **fields):
        identifier = max((m['local_id'] for m in self.rows), default=0)+1
        row = self.add(identifier, sender=sender, text=text, mention=False)
        row.update(fields)
        self.bot.tick()
        return row

    def decision(self, reply_to=None, *, speak=True, text=None, target=None):
        if reply_to is None:
            reply_to = [target or self.bot.participation.pending['source_ids'][-1]] if speak else []
        self.bot.participation.job.result = dict(ok=True, decision=dict(reply_to=reply_to))
        self.bot.tick()
        if text is not None and reply_to:
            self.generated(text)

    def confirm(self):
        worker = self.sends()[-1]
        worker.result = dict(ok=True, ui=dict(operation=dict(submitted=True)))
        return self.ordinary(worker.payload[2], sender='wxid_self')

    def enter(self):
        row = self.ordinary('ds，过来接个话')
        self.decision([row['local_id']], text='自然接话')
        self.confirm()

    def answer_mention(self, sender='wxid_friend'):
        identifier = max((m['local_id'] for m in self.rows), default=0)+1
        self.add(identifier, sender=sender)
        self.bot.tick()
        self.decision([identifier], text='正常回复真实提问')
        self.confirm()
