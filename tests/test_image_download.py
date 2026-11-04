"""Synthetic match ambiguity, source binding, cache confirmation and queue-worker boundaries."""
from tests.support.configuration import replace_config, update_config, snapshot
from tests.support.model_support import default_config
from contextlib import nullcontext
from contextlib import contextmanager
import copy
from pathlib import Path
import time
import unittest
from unittest.mock import Mock,patch

from wxbridge.config import default_document
from wechat_linux.desktop.image_download import DesktopImages,DownloadError,improve_image,similarity,unique_match
import tests.support.media as media


def candidate(a,b):
    score=similarity(a,b)
    return dict(bounds={'x':0,'y':0,'width':120,'height':120},cover=score,contain=score)


class ImageDownloadTests(unittest.TestCase):







    def test_multimodal_uses_upgraded_image_and_default_off_never_calls_ui(self):
        case=media.LocalImageFixture('runTest');case.setUp();self.addCleanup(case.doCleanups)
        case.path.write_bytes(media.png());generation=case.generation()
        with patch('wechat_linux.desktop.image_download.improve_image') as improve:
            _,info=case.prepare(generation)
            improve.assert_not_called();self.assertEqual(info['images'],1)
        case.cfg = replace_config(case.cfg, 'vision.download_enabled', True, owner=case)
        before=dict(mime='image/png',width=10,height=10,bytes=20,quality='thumbnail')
        after=dict(before,width=1000,height=1000,quality='cached')
        with patch('wechat_linux.media.images.read_local_image',return_value=(b'original-thumb',before)),\
             patch('wechat_linux.desktop.image_download.improve_image',return_value=(b'bound-full',after)):
            prompt,info=case.prepare(generation)
        self.assertEqual(info['items'][0]['width'],1000)
        self.assertTrue(prompt[1]['content'][-1]['image_url']['url'].endswith('Ym91bmQtZnVsbA=='))
