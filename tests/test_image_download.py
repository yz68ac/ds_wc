"""Synthetic match ambiguity, source binding, cache confirmation and queue-worker boundaries."""
from contextlib import contextmanager
import copy
import time
import unittest
from unittest.mock import Mock,patch

from wxbridge.config import DEFAULTS
from wxbridge.wechat.image_download import DesktopImages,DownloadError,improve_image,similarity,unique_match
import test_media as media


def candidate(a,b):
    score=similarity(a,b)
    return dict(bounds={'x':0,'y':0,'width':120,'height':120},cover=score,contain=score)


class ImageDownloadTests(unittest.TestCase):
    def test_match_requires_spatial_detail_and_unique_separation(self):
        image=[v for y in range(20) for x in range(20) for v in ((x*13)%256,(y*13)%256,((x+y)*7)%256)]
        near=[min(255,v+1) for v in image];other=list(reversed(image))
        good=candidate(image,near);bad=candidate(image,other)
        self.assertIs(unique_match([bad,good]),good)
        self.assertIsNone(unique_match([good,candidate(image,near)]))
        self.assertIsNone(unique_match([bad]))
        flat=[255,0,0]*400
        self.assertIsNone(unique_match([candidate(flat,flat)]))

    def test_stale_showing_frame_without_native_window_is_ignored(self):
        desktop=object.__new__(DesktopImages);desktop.deadline=time.monotonic()+2;desktop.ui=Mock()
        stale,live=Mock(),Mock();a,b=Mock(),Mock()
        desktop.visible=Mock(return_value=[(a,'page tab','Media',{}),(b,'page tab','Media',{})])
        desktop.ancestor_frame=Mock(side_effect=[stale,live])
        desktop.window=Mock(side_effect=[DownloadError('download_window_unavailable'),'123'])
        self.assertIs(desktop.frame_with('Media','page tab'),live)

    def test_click_refuses_wrong_active_window_before_mouse_operation(self):
        desktop=object.__new__(DesktopImages);desktop.deadline=time.monotonic()+2;desktop.x=Mock();desktop.window=Mock(return_value='123')
        desktop.x._xdotool.return_value.stdout=b'999'
        with patch('wxbridge.wechat.image_download.time.sleep'):
            with self.assertRaises(DownloadError):desktop.click(Mock(),dict(x=1,y=1,width=20,height=20))
        desktop.x.move_pointer.assert_not_called()
        self.assertFalse(any(c.args[0]=='click' for c in desktop.x._xdotool.call_args_list))

    def test_unproven_or_non_native_source_never_opens_ui(self):
        cfg=copy.deepcopy(DEFAULTS);cfg['ui']['backend']='native_service';adapter=Mock()
        for proof in ({},{'verified':'yes','chat_title':'synthetic','server_id':'10'},
                      {'verified':True,'chat_title':'','server_id':'10'}):
            data,info=improve_image({'download':proof},cfg,b'thumb',{'quality':'thumbnail'},time.monotonic()+1,adapter=adapter)
            self.assertEqual(data,b'thumb');self.assertEqual(info['download']['outcome'],'download_unverified')
        adapter.assert_not_called()

    def test_click_only_succeeds_after_original_bound_cache_confirms_and_closes_under_lock(self):
        cfg=copy.deepcopy(DEFAULTS);cfg['ui']['backend']='native_service'
        binding=dict(download=dict(verified=True,chat_title='synthetic',server_id='10'))
        original=dict(quality='thumbnail',width=20,height=20)
        desktop=Mock();events=[]
        @contextmanager
        def locked(*args):
            events.append('locked');yield;events.append('unlocked')
        desktop.close.side_effect=lambda:events.append('closed')
        with patch('wxbridge.wechat.image_download.download_lock',locked),\
             patch('wxbridge.ai.images.read_local_image',return_value=(b'full',dict(quality='cached',width=600,height=800))) as read:
            data,info=improve_image(binding,cfg,b'thumb',original,time.monotonic()+2,adapter=lambda *args:desktop)
        self.assertEqual(data,b'full');self.assertEqual(info['download']['outcome'],'downloaded')
        read.assert_called_once_with(binding,cfg)
        self.assertEqual(events,['locked','closed','unlocked'])

    def test_timeout_or_ambiguous_match_retains_correct_thumbnail_and_safe_error(self):
        cfg=copy.deepcopy(DEFAULTS);cfg['ui']['backend']='native_service'
        binding=dict(download=dict(verified=True,chat_title='synthetic',server_id='10'))
        desktop=Mock();desktop.open_matching.side_effect=DownloadError('download_match_ambiguous')
        @contextmanager
        def locked(*args):yield
        with patch('wxbridge.wechat.image_download.download_lock',locked),patch('wxbridge.ai.images.read_local_image') as read:
            data,info=improve_image(binding,cfg,b'correct-thumb',dict(quality='thumbnail'),time.monotonic()+1,adapter=lambda *args:desktop)
        self.assertEqual(data,b'correct-thumb');self.assertEqual(info['download']['outcome'],'download_match_ambiguous')
        desktop.close.assert_called_once();read.assert_not_called()

    def test_expired_budget_never_initializes_ui(self):
        cfg=copy.deepcopy(DEFAULTS);cfg['ui']['backend']='native_service';adapter=Mock()
        binding=dict(download=dict(verified=True,chat_title='synthetic',server_id='10'))
        data,info=improve_image(binding,cfg,b'correct-thumb',dict(quality='thumbnail'),time.monotonic()-1,adapter=adapter)
        self.assertEqual(info['download']['outcome'],'download_timeout')
        self.assertEqual(data,b'correct-thumb');adapter.assert_not_called()

    def test_multimodal_uses_upgraded_image_and_default_off_never_calls_ui(self):
        case=media.LocalImageTests('runTest');case.setUp();self.addCleanup(case.doCleanups)
        case.path.write_bytes(media.png());generation=case.generation()
        with patch('wxbridge.wechat.image_download.improve_image') as improve:
            _,info=case.prepare(generation)
            improve.assert_not_called();self.assertEqual(info['images'],1)
        case.cfg['vision']['download_enabled']=True
        before=dict(mime='image/png',width=10,height=10,bytes=20,quality='thumbnail')
        after=dict(before,width=1000,height=1000,quality='cached')
        with patch('wxbridge.ai.images.read_local_image',return_value=(b'original-thumb',before)),\
             patch('wxbridge.wechat.image_download.improve_image',return_value=(b'bound-full',after)):
            prompt,info=case.prepare(generation)
        self.assertEqual(info['items'][0]['width'],1000)
        self.assertTrue(prompt[1]['content'][-1]['image_url']['url'].endswith('Ym91bmQtZnVsbA=='))

    def test_download_config_rejects_invalid_flags_and_time_limits(self):
        import wxbridge.config as config
        from pathlib import Path
        for key,value in [('download_enabled',1),('download_timeout_seconds',False),
                          ('download_timeout_seconds',float('nan')),('download_timeout_seconds',float('inf')),
                          ('download_timeout_seconds',4),('download_timeout_seconds',31)]:
            defaults=copy.deepcopy(DEFAULTS);defaults['vision'][key]=value
            with patch.object(config,'DEFAULTS',defaults),patch.object(config,'load_dotenv'):
                with self.assertRaises(ValueError):config.load_config(Path(__file__).parent/'nonexistent.json')
