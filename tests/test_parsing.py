"""回复分片和提示词顺序的离线测试。"""
from __future__ import annotations
from tests.support.model_support import default_config
from contextlib import nullcontext

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wxbridge.wechat.messages import chunk_text
from wxbridge.config import DEFAULTS, _deep_merge
from wxbridge.ai.prompts import build_prompt
from tests.support.synthetic import msg


def test_chunk() -> None:
    chunks = chunk_text("a\n" + "b" * 4000, 1000)
    assert all(len(c) <= 1000 for c in chunks)
    assert "".join(chunks).replace("\n", "") == "a" + "b" * 4000


def test_prompt_order() -> None:
    cfg = default_config()
    ctx = [msg(1,900,'第一条','A'),msg(2,950,'第二条','B')]
    trig = msg(3,1000,'@DeepSeek 第三条','C')
    user = build_prompt(cfg, ctx, trig, "测试群")[1]["content"]
    assert user.index("第一条") < user.index("第二条") < user.index("@DeepSeek 第三条")


if __name__ == "__main__":
    test_chunk()
    test_prompt_order()
    print("OK: chunk/prompt")
