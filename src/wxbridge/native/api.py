"""Bot API facade over the application-owned native server."""
from wxbridge.native.server import NativeServer
from wxbridge.native.protocol import settings
from wxbridge.storage.memoryfs import require_memory_directory
from wxbridge.app.native_gateway import BotNativeService


class NativeAPI(NativeServer):
    def __init__(self,bot):
        self.bot=bot
        super().__init__(settings(bot.cfg),BotNativeService(bot),directory_validator=require_memory_directory)

    def handle(self,value):
        service=getattr(self,'service',None) or BotNativeService(self.bot,self.cfg)
        return service.handle(value)
