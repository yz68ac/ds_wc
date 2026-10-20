"""wxgf 内 HEVC 转 PNG；复用 Linux 系统库，在限时、限内存子进程中处理。"""
from __future__ import annotations

import ctypes as C
import ctypes.util
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import zlib

MAX_INPUT = 8 * 1024 * 1024
MAX_PIXELS = 16 * 1024 * 1024


class WxgfError(ValueError):
    pass


def _chroma(sps):
    """读取 SPS 中的 chroma_format_idc，区分彩色图像流与单色遮罩。"""
    rbsp = re.sub(b"\x00\x00\x03(?=[\x00-\x03])", b"\x00\x00", sps[2:])
    bit = 0
    def read(count):
        nonlocal bit
        if bit+count > len(rbsp)*8:
            raise WxgfError("invalid_wxgf")
        value = 0
        for _ in range(count):
            value = (value<<1) | ((rbsp[bit//8] >> (7-bit%8)) & 1)
            bit += 1
        return value
    def ue():
        zeroes = 0
        while not read(1):
            zeroes += 1
            if zeroes > 31:
                raise WxgfError("invalid_wxgf")
        return (1<<zeroes)-1+read(zeroes)
    read(4)
    layers = read(3)
    read(1)
    read(96)
    flags = [(read(1), read(1)) for _ in range(layers)]
    if layers:
        read(2*(8-layers))
    for profile,level in flags:
        if profile: read(88)
        if level: read(8)
    ue()
    chroma = ue()
    if chroma > 3:
        raise WxgfError("invalid_wxgf")
    return chroma


def extract_hevc(data):
    if not data.startswith(b"wxgf") or not 16 <= len(data) <= MAX_INPUT:
        raise WxgfError("invalid_wxgf")
    starts = list(re.finditer(b"\x00\x00(?:\x00)?\x01", data))
    if len(starts) > 4096:
        raise WxgfError("invalid_wxgf")
    vps = [i for i,m in enumerate(starts) if m.end()+2 <= len(data) and data[m.end()] == 0x40 and data[m.end()+1] & 7]
    streams = []
    for number,index in enumerate(vps):
        end_index = vps[number+1] if number+1 < len(vps) else len(starts)
        end = starts[end_index].start() if end_index < len(starts) else len(data)
        types,chroma,payload = set(),None,0
        for i in range(index,end_index):
            pos = starts[i].end()
            if pos+2 > len(data): continue
            kind = (data[pos] >> 1) & 63
            types.add(kind)
            nal_end = starts[i+1].start() if i+1 < end_index else end
            if kind == 33: chroma = _chroma(data[pos:nal_end])
            if kind < 32: payload += nal_end-pos
        if {32,33,34} <= types and payload and chroma is not None:
            streams.append((bool(chroma),payload,starts[index].start(),end))
    if streams:
        # wxgf 常先保存很小的单色遮罩；优先彩色流，再取有效负载较大的一组。
        _,_,start,end = max(streams)
        return data[start:end]
    raise WxgfError("invalid_wxgf")


def decode_wxgf(data, max_bytes):
    """只通过管道传递明文，子进程不继承 API Key 等业务环境变量。"""
    stream = extract_hevc(data)
    try:
        result = subprocess.run([sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--decode", str(max_bytes)],
            input=stream, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=8,
            env={"PATH": os.defpath, "LANG": "C.UTF-8"})
    except subprocess.TimeoutExpired:
        raise WxgfError("wxgf_decode_timeout") from None
    except OSError:
        raise WxgfError("wxgf_decoder_unavailable") from None
    errors = {20: "wxgf_decoder_unavailable", 21: "wxgf_decode_failed", 22: "image_dimension_limit", 23: "image_size_limit"}
    if result.returncode:
        raise WxgfError(errors.get(result.returncode, "wxgf_decoder_failed"))
    if len(result.stdout) > max_bytes:
        raise WxgfError("image_size_limit")
    if not result.stdout.startswith(b"\x89PNG\r\n\x1a\n"):
        raise WxgfError("wxgf_decode_failed")
    return result.stdout


def _library(name):
    try:
        path = C.util.find_library(name)
        if path:
            return C.CDLL(path)
    except (OSError, AttributeError):
        pass
    raise WxgfError("wxgf_decoder_unavailable")


def _function(lib, name, result, *args):
    try:
        fn = getattr(lib, name)
    except AttributeError:
        raise WxgfError("wxgf_decoder_unavailable") from None
    fn.restype, fn.argtypes = result, list(args)
    return fn


def _png(rgb, width, height, max_bytes):
    def chunk(name, data):
        return struct.pack(">I", len(data))+name+data+struct.pack(">I", zlib.crc32(name+data)&0xFFFFFFFF)
    compressor = zlib.compressobj(6)
    parts, size = [], 0
    for row in range(height):
        part = compressor.compress(b"\0"+rgb[row*width*3:(row+1)*width*3])
        parts.append(part)
        size += len(part)
        if size+100 > max_bytes:
            raise WxgfError("image_size_limit")
    parts.append(compressor.flush())
    image = (b"\x89PNG\r\n\x1a\n"+chunk(b"IHDR", struct.pack(">IIBBBBB", width,height,8,2,0,0,0))
             +chunk(b"IDAT", b"".join(parts))+chunk(b"IEND", b""))
    if len(image) > max_bytes:
        raise WxgfError("image_size_limit")
    return image


def _decode(stream, max_bytes):
    """仅使用公开 C 函数和不透明指针，不依赖解码器内部结构布局。"""
    decoder = _library("de265")
    scaler = _library("swscale")
    util = _library("avutil")
    pointer, integer = C.c_void_p, C.c_int
    byte_p = C.POINTER(C.c_ubyte)
    int_p = C.POINTER(integer)
    new = _function(decoder,"de265_new_decoder",pointer)
    free = _function(decoder,"de265_free_decoder",integer,pointer)
    push = _function(decoder,"de265_push_data",integer,pointer,pointer,integer,C.c_int64,pointer)
    flush = _function(decoder,"de265_flush_data",integer,pointer)
    decode = _function(decoder,"de265_decode",integer,pointer,int_p)
    peek = _function(decoder,"de265_peek_next_picture",pointer,pointer)
    width_of = _function(decoder,"de265_get_image_width",integer,pointer,integer)
    height_of = _function(decoder,"de265_get_image_height",integer,pointer,integer)
    chroma_of = _function(decoder,"de265_get_chroma_format",integer,pointer)
    bits_of = _function(decoder,"de265_get_bits_per_pixel",integer,pointer,integer)
    plane_of = _function(decoder,"de265_get_image_plane",byte_p,pointer,integer,int_p)
    range_of = _function(decoder,"de265_get_image_full_range_flag",integer,pointer)
    matrix_of = _function(decoder,"de265_get_image_matrix_coefficients",integer,pointer)
    pix_fmt = _function(util,"av_get_pix_fmt",integer,C.c_char_p)
    context = _function(scaler,"sws_getContext",pointer,integer,integer,integer,integer,integer,integer,integer,pointer,pointer,pointer)
    scale = _function(scaler,"sws_scale",integer,pointer,C.POINTER(byte_p),int_p,integer,integer,C.POINTER(byte_p),int_p)
    coefficients = _function(scaler,"sws_getCoefficients",int_p,integer)
    colors = _function(scaler,"sws_setColorspaceDetails",integer,pointer,int_p,integer,int_p,integer,integer,integer,integer)
    free_scale = _function(scaler,"sws_freeContext",None,pointer)
    ctx = new()
    if not ctx:
        raise WxgfError("wxgf_decode_failed")
    sws = None
    try:
        source = C.create_string_buffer(stream)
        if push(ctx,source,len(stream),0,None) or flush(ctx):
            raise WxgfError("wxgf_decode_failed")
        image = None
        for _ in range(256):
            more = integer()
            error = decode(ctx,C.byref(more))
            if error:
                raise WxgfError("wxgf_decode_failed")
            image = peek(ctx)
            if image or not more.value:
                break
        if not image:
            raise WxgfError("wxgf_decode_failed")
        width,height = width_of(image,0),height_of(image,0)
        if not 0 < width <= 8192 or not 0 < height <= 8192 or width*height > MAX_PIXELS:
            raise WxgfError("image_dimension_limit")
        chroma,bits = chroma_of(image),bits_of(image,0)
        formats = {0:"gray",1:"yuv420p",2:"yuv422p",3:"yuv444p"}
        if chroma not in formats or bits not in (8,10,12):
            raise WxgfError("wxgf_decode_failed")
        name = formats[chroma]+(str(bits)+"le" if bits > 8 else "")
        src_fmt,dst_fmt = pix_fmt(name.encode()),pix_fmt(b"rgb24")
        if src_fmt < 0 or dst_fmt < 0:
            raise WxgfError("wxgf_decode_failed")
        sws = context(width,height,src_fmt,width,height,dst_fmt,2,None,None,None)
        if not sws:
            raise WxgfError("wxgf_decode_failed")
        matrix = matrix_of(image)
        coeff = coefficients(matrix if matrix in (1,9) else 5)
        if colors(sws,coeff,int(bool(range_of(image))),coeff,1,0,1<<16,1<<16) < 0:
            raise WxgfError("wxgf_decode_failed")
        planes,strides = (byte_p*4)(),(integer*4)()
        for channel in range(1 if chroma == 0 else 3):
            stride = integer()
            planes[channel] = plane_of(image,channel,C.byref(stride))
            strides[channel] = stride.value
            if not planes[channel] or stride.value <= 0:
                raise WxgfError("wxgf_decode_failed")
        rgb = (C.c_ubyte*(width*height*3+64))()
        out_planes,out_strides = (byte_p*4)(),(integer*4)()
        out_planes[0],out_strides[0] = C.cast(rgb,byte_p),width*3
        if scale(sws,planes,strides,0,height,out_planes,out_strides) != height:
            raise WxgfError("wxgf_decode_failed")
        return _png(bytes(rgb)[:width*height*3],width,height,max_bytes)
    finally:
        if sws: free_scale(sws)
        free(ctx)


def _main():
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CORE,(0,0))
        resource.setrlimit(resource.RLIMIT_FSIZE,(0,0))
        resource.setrlimit(resource.RLIMIT_CPU,(5,5))
        resource.setrlimit(resource.RLIMIT_AS,(768*1024*1024,768*1024*1024))
        limit = int(sys.argv[2])
        if not 1 <= limit <= MAX_INPUT:
            return 23
        stream = sys.stdin.buffer.read(MAX_INPUT+1)
        if len(stream) > MAX_INPUT:
            return 23
        image = _decode(stream,limit)
        sys.stdout.buffer.write(image)
        return 0
    except WxgfError as exc:
        return {"wxgf_decoder_unavailable":20,"image_dimension_limit":22,"image_size_limit":23}.get(str(exc),21)
    except (ImportError,OSError,ValueError,MemoryError):
        return 21


if __name__ == "__main__":
    raise SystemExit(_main() if len(sys.argv)==3 and sys.argv[1]=="--decode" else 21)
