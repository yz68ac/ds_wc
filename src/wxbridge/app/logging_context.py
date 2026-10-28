"""Append a validated logical instance ID without changing existing event names."""
import logging


class InstanceLog(logging.LoggerAdapter):
    def process(self, message, kwargs):
        field = 'instance='+self.extra['instance']
        if 'metadata=' in message:
            return message.replace('metadata=', field+' metadata=', 1), kwargs
        return message+' '+field, kwargs
