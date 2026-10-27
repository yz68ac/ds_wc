# 脚本地图

所有示例均从仓库根目录执行。`python3 -m wxbridge` 是应用命令入口；这里的脚本用于环境准备、离线检查、发布和显式维护。

```text
scripts/
  test-offline.py、check-repository.py、release.py  开发与发布
  run-live.py                                    现有监听服务入口
  boot-session.py、boot-recovery.py               开机与会话恢复
  start-headless.sh、stop-headless.sh             虚拟显示会话
  login-vnc.sh、configure-deepseek.sh              登录查看与凭据配置
  install-native-helper.py                       隔离原生 helper 安装
  systemd/                                      服务与内存挂载模板
  diagnostics/                                  数据库诊断与显式 API 验证
  maintenance/                                  针对旧故障的受控修复
```

## 日常开发

```bash
python3 -X utf8 scripts/test-offline.py
python3 scripts/check-repository.py --history
git diff --check
```

这些检查不需要微信账号或 API Key。发布包由 `release.py build` 从干净的已提交代码构建，`release.py verify` 校验清单和文件；完整步骤见 [部署说明](../docs/DEPLOYMENT.md)。不要把带私密数据的整个工作目录打包上传。

## Linux 启动与运维

| 入口 | 用途与边界 |
| --- | --- |
| `run-live.py` | 转入相同 CLI/runtime，并显式启用发送；现有 systemd 路径保留 |
| `boot-session.py` | 前台监督微信进程，调用虚拟显示启动脚本 |
| `boot-recovery.py` | 内存目录、登录账号、密钥、检查点和监听恢复；由专用服务运行 |
| `start-headless.sh` / `stop-headless.sh` | 启停专用 Xvfb 会话；停止会话会影响微信 |
| `login-vnc.sh` | 临时查看登录界面，通过 SSH 隧道访问 |
| `configure-deepseek.sh` | 在本机交互配置 API Key，不把 Key 作为命令行参数 |
| `install-native-helper.py` | 首次安装 root 所有的 helper、固定版本 agent/profile 和隔离 Frida 环境；拒绝覆盖已有安装 |
| `systemd/` | 服务与私有内存挂载模板，实际部署需核对用户、路径及服务依赖 |

参照 [开机恢复](../docs/BOOT_RECOVERY.md) 与 [原生发送](../docs/NATIVE_API.md) 配置。后台发送已脱离会话选择，现有客户端启动与恢复仍有显示和辅助功能依赖。

## 按需诊断

| 新路径 | 是否联网 / 操作 |
| --- | --- |
| `diagnostics/diagnose-intake.py` | 读取真实数据库和任务元数据；不调用模型、不发送消息，先停止监听及恢复定时器 |
| `diagnostics/diagnose-media.py` | 读取图片附件索引、缓存和资源结构；同样先停止监听及恢复定时器，输出仍可能包含私密元数据 |
| `diagnostics/verify-search-api.py --live-api` | 显式调用模型及搜索，会产生费用；不读取微信、不向群发送消息 |
| `diagnostics/verify-vision-api.py --synthetic-api` | 显式把合成色块发给配置的模型，会产生费用；不读取真实图片、不向群发送消息 |

诊断脚本移动后仍从自身位置定位仓库根目录、`config.json` 和提示词，不依赖调用者当前目录。它们不会因离线测试或导入而自动执行 API 验证。

运行中只想看状态，优先使用 `python3 -m wxbridge status --once` 或 `queue-status`，无需刷新解密数据库。交互面板仅在显式打开时采集，详见 [状态说明](../docs/STATUS.md)。

## 历史故障维护

`maintenance/recover-focus-only.py` 只处理旧版把 `SetFocus` 当发送动作的特定缺陷，会核对未决意图、发出记录、当前群和完全一致的草稿，并可能修改任务状态。它不是常规重试工具，不适用于原生发送结果不明，也不适用于真实点击后的超时。原有合成回归保留在 `tests/test_recover_focus.py`。

本轮只调整辅助脚本的分类；所有 systemd 使用的启动路径、应用命令和原生 helper 资产位置保持兼容。
