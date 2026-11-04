# 微信适配库依赖

`wechat_linux` 由独立的本地 Git 仓库 `wechat-linux-adapter` 维护，机器人固定依赖 `wechat-linux-adapter==0.3.3`。独立仓库位于工作区同级目录 `../wechat-linux-adapter`；它有自己的接口文档、测试、版本标签和 wheel 构建流程。机器人从适配库顶层及 `storage`、`messages`、`media`、`native`、`desktop`、`runtime` 的公共包入口导入能力；具体导出见独立库的 `docs/API.md`。此版本修复了私聊引用图片快照发送者与原消息记录不一致时的绑定：仍要求同会话唯一消息 ID 与图片摘要吻合。它也支持在核验后的 V2 JPEG 缩略图末尾移除最多 32 字节的微信尾部。

机器人不再包含适配器源码。`dependencies/` 中的 wheel 是已经发布到本地的固定版本；`wechat-linux-adapter.json` 记录版本、源码提交、标签和 SHA-256。Docker、离线构建和发布从这一产物读取适配器，不依赖同级源码目录。应用正常安装使用 `uv sync --locked`，构建和安装脚本会验证 wheel 的清单、内容和原生资源。

## 更新依赖

在独立库完成修改、测试并创建干净版本标签后构建 wheel，然后在机器人仓库执行：

```bash
python scripts/update-wechat-adapter.py ../wechat-linux-adapter/dist/wechat_linux_adapter-版本号-py3-none-any.whl --source-repo ../wechat-linux-adapter
python scripts/dependencies.py
uv sync --locked --extra database --extra login --group test
python -X utf8 scripts/test-offline.py
```

更新脚本要求 wheel 的源码与所指向的干净标签一致，并拒绝同一版本号对应不同内容。检查 `pyproject.toml`、`uv.lock`、依赖清单与 wheel 的变更后再提交机器人仓库。

## 部署边界

Python wheel、系统密钥恢复扫描器和 root helper 是不同的安装资产。安装/升级流程会从校验后的 wheel 获取适配器资源；helper 在系统中独立安装。若新版本更改 `agent.js`、`image_agent.js`、C 适配器或 profile，必须审阅并同步更新已安装 helper，不能只更新 Python 环境。旧 source-only 更新会拒绝依赖包变更。

当前适配档案针对 Linux x86_64 微信 4.1.13.26。文字和图片的 `SUBMITTED` 回执只表示请求已提交，机器人负责队列和发送确认。接口、登录、图片和离线分析的详细说明以独立库的 `README.md` 和 `docs/` 为准。
