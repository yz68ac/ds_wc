"""Application dry-run policy around the desktop adapter."""
import logging

from wechat_linux.desktop.ui import (
    A11y, Clipboard, Sender as DesktopSender, UIError, WrongChatError, X11, chat_label_matches,
)


class Sender(DesktopSender):
    def __init__(self, cfg):
        options = dict(cfg['ui'])
        self.dry_run = options.pop('dry_run', False)
        super().__init__(options)

    def status(self):
        return dict(super().status(), dry_run=self.dry_run)

    def send(self, chat, text):
        if self.dry_run:
            logging.getLogger('wxbridge').info('Dry-run submission chars=%s', len(text))
            return None
        return super().send(chat, text)
