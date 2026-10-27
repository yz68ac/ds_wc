"""Linux 连续监听入口：真实 @、可配置上下文、恢复内存队列。

显式执行本脚本会启用发送。导入模块只定义入口，不启动客户端或监听。
"""
from __future__ import annotations

from pathlib import Path
import sys


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))

    from wxbridge.cli import main as cli_main

    # 保留既有服务入口；实际启动策略统一由 CLI → runtime 执行。
    return cli_main(["--config", str(root / "config.json"), "run", "--send"])


if __name__ == "__main__":
    raise SystemExit(main())
