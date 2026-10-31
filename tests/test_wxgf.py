"""合成32×16色块验证HEVC解码、遮罩选择与失败隔离，无真实图片。"""
import base64
import re
import struct
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch
import zlib

from wechat_linux.media.wxgf import WxgfError, _chroma, decode_wxgf, extract_hevc
from wxbridge.ai.images import validate_image

# 现有 FFmpeg/libx265 生成的红色静态图；只保留 VPS/SPS/PPS/IDR，无外部素材。
RED = base64.b64decode('AAAAAUABDAH//wFgAAADAJAAAAMAAAMAHpWYCQAAAAFCAQEBYAAAAwCQAAADAAADAB6gQhFllZqvK8BaAgAAAwACAAADADIQAAAAAUQBwXPQiQAAAAEoAa8dgPAOCP+JeffqFlV+eA==')
MASK = base64.b64decode('AAAAAUABDAH//wQIAAADAJ/IAAADAAAelZgJAAAAAUIBAQQIAAADAJ/IAAADAAAewQhFllZqvK8BbAgAAAMACAAAAwDIQAAAAAFEAcFz0IkAAAABKAGuO8UmDjsx+8A=')


def container(stream=RED):
    return b'wxgf'+bytes(8)+stream


class WxgfTests(unittest.TestCase):
    def test_extract_requires_complete_hevc_parameter_sets_and_frame(self):
        self.assertEqual(extract_hevc(container()), RED)
        for data in (b'not-a-container', container(b'\0\0\0\1\x40\1'), container(RED[:30]), container(RED[:-21])):
            with self.subTest(size=len(data)), self.assertRaises(WxgfError):
                extract_hevc(data)

    def test_chroma_detects_color_and_rejects_truncated_sps(self):
        sps = re.split(b'\0\0\0\1', RED)[2]
        self.assertEqual(_chroma(sps), 1)
        with self.assertRaises(WxgfError):
            _chroma(sps[:5])

    def test_color_stream_selected_instead_of_first_mask_even_if_mask_is_larger(self):
        self.assertEqual(_chroma(re.split(b'\0\0\0\1',MASK)[2]),0)
        self.assertEqual(extract_hevc(container(MASK+b'X'*500+RED)),RED)

    def test_transport_uses_pipes_timeout_and_no_business_environment(self):
        with patch('wechat_linux.media.wxgf.subprocess.run',return_value=Mock(returncode=0,stdout=b'\x89PNG\r\n\x1a\nsynthetic')) as run:
            decode_wxgf(container(),1000)
        args = run.call_args.kwargs
        self.assertEqual(args['input'],RED)
        self.assertEqual(args['timeout'],8)
        self.assertEqual(set(args['env']),{'PATH','LANG'})
        self.assertNotIn('DEEPSEEK_API_KEY',args['env'])

    def test_timeout_missing_decoder_and_size_fail_with_safe_codes(self):
        cases = [(subprocess.TimeoutExpired('PRIVATE',8),'wxgf_decode_timeout'),
                 (Mock(returncode=20,stdout=b''),'wxgf_decoder_unavailable'),
                 (Mock(returncode=23,stdout=b''),'image_size_limit'),
                 (Mock(returncode=-11,stdout=b'PRIVATE'),'wxgf_decoder_failed'),
                 (Mock(returncode=0,stdout=b'X'*100),'image_size_limit')]
        for result,code in cases:
            with self.subTest(code=code), patch('wechat_linux.media.wxgf.subprocess.run',side_effect=result if isinstance(result,Exception) else None,return_value=result):
                with self.assertRaisesRegex(WxgfError,'^'+code+'$'):
                    decode_wxgf(container(),50)

    @unittest.skipUnless(sys.platform.startswith('linux'),'实际系统解码库仅在Linux验证')
    def test_real_native_decode_keeps_size_and_red_pixels(self):
        png = decode_wxgf(container(MASK+RED),1024*1024)
        self.assertEqual(validate_image(png,1024*1024),('image/png',32,16))
        pos=8;compressed=b''
        while pos+12 <= len(png):
            size=struct.unpack_from('>I',png,pos)[0]
            if png[pos+4:pos+8] == b'IDAT': compressed+=png[pos+8:pos+8+size]
            pos+=size+12
        pixels=zlib.decompress(compressed)
        self.assertEqual(pixels[0],0)
        self.assertGreater(pixels[1],pixels[2]+100)
        self.assertGreater(pixels[1],pixels[3]+100)
