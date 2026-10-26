# 对应微信客户端的备用资源

这里保存与 `wechat_linux` 原生 profile 对应的本地分析样本和离线安装包，便于后续复核函数、重新导入 Ghidra 或恢复同版客户端。

| 平台 | 版本 | 资源与核验记录 |
| --- | --- | --- |
| Linux x86_64 / Debian amd64 | 4.1.13.26 | [版本说明](linux-x86_64/4.1.13.26/README.md)、[清单](linux-x86_64/4.1.13.26/manifest.json)、[SHA256SUMS](linux-x86_64/4.1.13.26/SHA256SUMS) |

目录布局：

```text
resources/wechat/linux-x86_64/4.1.13.26/
  README.md
  manifest.json
  SHA256SUMS
  binary/wechat                        Linux ELF 主程序，本地备用
  installer/WeChatLinux_x86_64.deb       原始安装包，本地备用
```

文档、manifest 和校验清单随源码保存；`binary/`、`installer/` 被 Git 忽略，不进入普通源码发布包或 Python wheel。克隆仓库只会获得清单，实际文件需另外复制这份资源目录。程序不会自动执行、安装或替换这些资源，运行时仍检查实际微信进程和 profile。

主程序约 173 MiB，原始 deb 约 221 MiB。现有应用源码发布包有 32 MiB 总量限制，故大文件作为本地独立资源保存。若之后需要与朋友分发，单独传输资源目录，并用清单复核；不要只把校验清单误当作已附带二进制。

来源是用户此前提供的 deb；本次核验的是本地样本一致性。资源不包含账号、登录状态、数据库、聊天或密钥。技术背景见 [研究报告](../../docs/WECHAT_RESEARCH_REPORT.md)，调用方式见 [能力库说明](../../docs/WECHAT_LIBRARY.md)。
