# Linux 微信 4.1.13.26 x86_64 备用样本

核验日期：2026-10-26。此目录对应公共库当前 `service_v1` profile，供本地分析和离线备用；原归档文件保留。

## 文件与用途

| 文件 | 字节数 | 用途 |
| --- | --- | --- |
| `binary/wechat` | 181208032 | 完整、未修改的 ELF64 little-endian x86_64 主程序，供 Ghidra 或 hash 对照 |
| `installer/WeChatLinux_x86_64.deb` | 231861976 | 原始 Debian 软件包，内部 Version 为 4.1.13.26、Architecture 为 amd64 |

主程序 SHA-256：

```text
2749535f299ba67feb8ef80cab898bb0531fa8f376b16f8a72db706811c84132
```

deb SHA-256：

```text
a0db8caeee8d1e6cfe0aa5ce58710270f8d74a90ff4920f53a3fc22aa0956fe9
```

实际校验包括：主程序 ELF 架构、复制前后 hash、deb 控制信息，以及流式读取 deb 中 `opt/wechat/wechat` 得到的 hash。三处主程序均与 [原生 profile](../../../../src/wechat_linux/native/profile.json) 一致。没有执行样本、安装软件或修改线上微信。

来源是用户原先下载并提供的包；上述 hash 用于确认内容一致，不是本次取得的腾讯签名验证结果。精确文件信息见 [manifest.json](manifest.json)。

## 复制到其它机器后复核

保持 `binary/`、`installer/` 与清单的相对目录，在 Linux 进入本版本目录运行：

```bash
sha256sum -c SHA256SUMS
```

Windows PowerShell 可分别计算：

```powershell
Get-FileHash -Algorithm SHA256 -LiteralPath .\binary\wechat
Get-FileHash -Algorithm SHA256 -LiteralPath .\installer\WeChatLinux_x86_64.deb
```

主程序单文件用于分析，不代表完整客户端安装；恢复安装使用 deb 及其系统依赖。SDK 不会自动加载这里的样本，helper 仍核验实际运行进程的主程序。

这些大文件仅在本地资源目录保存，由 Git 忽略；克隆源码和安装 wheel 不会自动获得它们。不要用这里的二进制覆盖运行中的客户端来跳过版本检查。
