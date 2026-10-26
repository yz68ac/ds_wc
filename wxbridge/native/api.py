"""Legacy bot-bound facade. New projects use wechat_linux.transport.server.NativeServer."""
from wechat_linux.transport.server import NativeServer
from wechat_linux.native.protocol import settings
from wechat_linux.runtime.memoryfs import require_memory_directory
from wxbridge.app.native_gateway import BotNativeService


class NativeAPI(NativeServer):
    def __init__(self,bot):
        self.bot=bot
        super().__init__(settings(bot.cfg),BotNativeService(bot),directory_validator=require_memory_directory)

    def handle(self,value):
        service=getattr(self,'service',None) or BotNativeService(self.bot,self.cfg)
        return service.handle(value)
