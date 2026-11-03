"""Bot API facade over the application-owned native server."""
from wxbridge.submission_api.server import SubmissionServer
from wxbridge.submission_api.protocol import settings
from wxbridge.storage.memoryfs import require_runtime_directory as require_memory_directory
from wxbridge.app.delivery.submission_gateway import ConversationSubmissionService


class SubmissionAPI(SubmissionServer):
    def __init__(self,bot):
        self.bot=bot
        super().__init__(settings(bot.cfg),ConversationSubmissionService(bot),directory_validator=require_memory_directory)

    def handle(self,value):
        service=getattr(self,'service',None) or ConversationSubmissionService(self.bot,self.cfg)
        return service.handle(value)
