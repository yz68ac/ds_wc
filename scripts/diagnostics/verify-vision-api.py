"""显式调用当前配置模型识别合成色块；不读取微信，不发送群消息。"""
import base64
import json
from pathlib import Path
import struct
import sys
import zlib

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from wxbridge.config import load_config
from wxbridge.ai.client import DeepSeekClient


def main():
    if "--synthetic-api" not in sys.argv:
        raise SystemExit("请显式传入 --synthetic-api；将调用配置的 API 并产生少量费用")
    cfg = load_config(ROOT/"config.json")
    cfg["deepseek"]["max_tokens"] = 40
    def chunk(kind, data):
        return struct.pack(">I", len(data))+kind+data+struct.pack(">I", zlib.crc32(kind+data)&0xffffffff)
    width, height = 128, 64
    pixels = (b"\0"+b"\xff\0\0"*64+b"\0\0\xff"*64)*height
    data = (b"\x89PNG\r\n\x1a\n"+chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            +chunk(b"IDAT", zlib.compress(pixels))+chunk(b"IEND", b""))
    prompt = [dict(role="user", content=[
        dict(type="text", text="按从左到右顺序，说明图片两半各是什么颜色。只输出颜色名称，不解释。"),
        dict(type="image_url", image_url=dict(url="data:image/png;base64,"+base64.b64encode(data).decode(), detail="original"))])]
    try:
        answer = DeepSeekClient(cfg).chat(prompt)
        passed = "红" in answer and "蓝" in answer and answer.index("红") < answer.index("蓝")
        print(json.dumps(dict(model=cfg["deepseek"]["model"], synthetic_image=True,
                              api_vision_passed=passed, answer_chars=len(answer)), ensure_ascii=False))
        return 0 if passed else 1
    except Exception as exc:
        print(json.dumps(dict(api_vision_passed=False, error=type(exc).__name__,
                              http_status=getattr(exc.__cause__, "code", None))))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
