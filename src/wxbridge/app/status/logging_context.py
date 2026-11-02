"""Configure runtime logging and attach validated instance IDs to existing events."""
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

log = logging.getLogger("wxbridge")


class InstanceLog(logging.LoggerAdapter):
    def process(self, message, kwargs):
        field = 'instance='+self.extra['instance']
        if 'metadata=' in message:
            return message.replace('metadata=', field+' metadata=', 1), kwargs
        return message+' '+field, kwargs


def setup_logging(cfg, verbose=True):
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    for handler in log.handlers[:]:
        handler.close()
        log.removeHandler(handler)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%m-%d %H:%M:%S")
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    log.addHandler(stream)
    try:
        Path(cfg.account.log_file).parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(cfg.account.log_file, maxBytes=2 * 1024 * 1024,
                                 backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except OSError:
        pass
