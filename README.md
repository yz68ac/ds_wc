# wxbridge

## 简介

wxbridge 是运行在 Linux 微信上的 AI 机器人，支持群聊、私聊、图片理解与生成、引用改图、长期记忆和 Web 管理。

一个微信账号对应一个容器，内部可创建多个独立会话实例。Docker 固定程序和依赖版本，账号登录、配置及长期数据保存在外部目录，方便在其他主机部署和升级。

默认直连模型服务，不要求代理或 CPA。容器功能当前位于 `feat/account-container-runtime` 分支，尚未迁移现网。

## 快速开始

需要 **Linux amd64、Docker Engine 和 Compose 插件**，内核须支持 `tmpfs,noswap`（已在 Debian 13 / Linux 6.12 验收）。宿主机无需安装 Python、Node 或微信。

### 1. 启动

在仓库根目录执行：

```bash
docker compose up -d --build
```

首次构建需要联网，并准备仓库兼容的微信安装包：

```text
resources/wechat/linux-x86_64/4.1.13.26/installer/WeChatLinux_x86_64.deb
```

镜像会校验安装资源并构建依赖。已有镜像可用 `docker save` / `docker load` 分发到其他同架构主机，再执行 `docker compose up -d --no-build`。当前尚未提供公开镜像。

Compose 自动创建 `data/`，设置初始目录权限并生成管理台密码。短暂运行的 `init` 服务完成后退出，长期运行的是 `account` 容器。若 Docker 要求管理员权限，在命令前加 `sudo`。

### 2. 填写模型密钥

编辑 `provider.env`，填写所用服务的密钥，例如：

```bash
sudoedit provider.env
```

```dotenv
DEEPSEEK_API_KEY=你的密钥
```

默认使用官方接口。需要网关或代理时，再在同一文件填写对应 Provider 的 `BASE_URL` 或 `PROXY_URL`，如 `OPENAI_BASE_URL`、`OPENAI_PROXY_URL`。默认使用 host 网络，与 Linux 宿主机共享网络；宿主机代理可直接填写 `http://127.0.0.1:10808`，本机 CPA 可填写 `http://127.0.0.1:8317`。无需 `extra_hosts`、端口映射或额外代理转发；不使用代理时留空即可。

部署根目录只读挂载以读取 `provider.env`，支持原子替换；密钥不写入镜像。容器可读取部署目录中其他具有读取权限的文件，请勿在该目录混放无关私密文件。普通编辑可保留原权限；原子替换时需保留 `root:10001`、`0440` 的文件读取权限。

### 3. 打开管理台

访问 **http://127.0.0.1:8730**，初始管理密码在 `data/settings/web-password`。

```bash
sudo cat data/settings/web-password
```

远程服务器默认仅允许本机访问，可在自己的电脑建立 SSH 隧道后打开同一地址：

```bash
ssh -L 8730:127.0.0.1:8730 用户名@服务器地址
```

在管理台完成以下操作：

1. 在工作台点击“查看 / 刷新登录二维码”，扫码并完成手机确认。
2. 在“会话实例”创建实例，填写目标会话的稳定 ID、选择模型并启用。可先用 `filehelper`（文件传输助手）测试；群聊还需配置参与模型。模型目录可在填写密钥后刷新。
3. 回到工作台，点击“绑定账号与实例会话”，再到“服务管理”启动机器人。

新增其他会话后，先在服务管理停止机器人，再绑定一次，使新会话获得 helper 发送授权。绑定操作不会自动启动机器人。

### 日常使用

| 操作 | 方式 |
| --- | --- |
| 修改实例、提示词 | 管理台保存并应用，无需重启容器 |
| 修改模型密钥、网关或代理 | 修改 `provider.env`，在服务管理应用机器人配置 |
| 查看状态、启停机器人 | 管理台“服务管理” |
| 查看容器日志 | `docker compose logs --tail 100 -f account` |
| 停止整套环境 | `docker compose stop` |
| 重启整套环境 | `docker compose restart account` |
| 升级镜像 | 换用新镜像后执行 `docker compose up -d` |

完整重启或重建容器会清空队列、游标和未完成任务，以最新消息重新建立基线，不补回复旧消息。仅应用配置或重启内部机器人会保留内存队列和游标。微信登录与长期数据保留，微信要求手机确认时按提示操作。

备份或迁移时保留根目录的 `provider.env` 和整个 `data/` 目录及权限。同一份账号数据只能由一个运行环境使用。第二个账号应使用独立部署目录、Compose 项目名和端口；可用 `WXBRIDGE_WEB_PORT` 设置各账号管理台的实际监听端口，例如 `WXBRIDGE_WEB_PORT=8731 docker compose -p account2 up -d`（启动时覆盖已保存的端口）；`WXBRIDGE_IMAGE` 指定镜像。

## 功能介绍

- **多会话实例**：群聊、私聊各自配置模型、提示词、启停状态和记忆；实例可独立应用配置。
- **模型接入**：支持 OpenAI、Anthropic、DeepSeek、Gemini 的现有协议适配，以及相应兼容网关；代理按需配置。
- **AI 能力**：自动回复、群聊参与判断、上下文管理、长期记忆和关系学习，具体取决于模型与实例设置。
- **图片能力**：理解引用图片，可选自动附带会话窗口中的图片；支持 OpenAI Images 和 Gemini 文生图，以及引用原图后的修改。
- **微信接入**：读取数据库、会话、消息和媒体，通过原生 helper 提交文本、图片并协调发送确认。兼容档案针对微信 `4.1.13.26 x86_64`。
- **运行管理**：集成虚拟桌面、扫码登录、恢复和有序停机；Compose 停机宽限期为 10 分钟。未扫码不会仅因未登录而反复重启。
- **数据隔离**：配置、登录和长期记忆持久化；队列使用私有 tmpfs；解密库及扫描出的数据库密钥使用 `tmpfs,noswap`，验证失败拒绝启动。
- **网络**：账号容器采用 host 网络，仍保留私有 PID、IPC 和文件系统。管理台默认只监听 `127.0.0.1`；从旧版迁移时会将原来的 `0.0.0.0` 改为 loopback。多个账号共享主机端口，需选择不同端口。
- **管理台与 CLI**：Web 保留认证及 Host/Origin 检查；容器内可通过 `docker compose exec account wxbridge status` 等命令运维。现有 systemd 后端继续保留原行为。

### 图片上下文与引用改图

图片理解使用实例的回复模型，该模型需支持图片输入。在管理台实例配置中打开“自动将图片加入上下文”，或在实例 JSON 中设置以下字段，再保存并应用：

```json
{
  "vision": {
    "enabled": true,
    "context_images_enabled": true,
    "max_images": 4
  }
}
```

`vision.enabled` 默认开启，`vision.context_images_enabled` 默认关闭。关闭自动上下文时，仍可通过微信“引用”图片发问；开启后，会从当前会话保留的上下文中附带图片，不扩大历史读取范围。引用图片优先，其余按最近图片补充，共用每次最多 4 张、每张最多 8 MiB 的限制；读不到图片时会标明不可用。

生图和改图还需开启 `image_generation.enabled`，选择 `model_clients.image` 并填写 `models.image.model`。回复模型需支持工具调用，改图时还需能读取引用图片。图片连接可选 `openai` 或 `gemini`，具体配置见[实例示例](examples/instance/config.example.jsonc)和 [Provider 用法](docs/PROVIDER.md)。

修改图片时，在微信中引用要修改的那张图片，再描述要求，例如“把背景改成雪山”。每轮最多一个图片任务；只有本轮实际读到的引用图片可作为改图来源，普通历史图片不会自动作为改图原图。原图不可用时需重新引用；编辑接口失败也不会自动切换成文生图。

机器人生成的图片在确认发出后会按上下文窗口有限保留，供后续识图或引用修改；这不是长期图片记忆，重建账号容器会清空临时索引。图片输入的用量不包含在 `target.context_max_chars` 文本字符预算中，同一图片在后续请求中再次附带也会产生输入用量，实际以模型服务返回的 usage 为准。

### 配置与开发资料

`data/config/` 保存账号与实例配置，`data/home/` 保存微信登录、原始数据库和记忆，根目录 `provider.env` 保存 Provider 配置与密钥，`data/settings/` 保存管理台及 helper 设置，`data/service/` 保存启停状态，`data/catalog/` 保存模型目录缓存。

账号配置的 `helper_transport.enabled` 控制微信 helper 发送，`submission_api.enabled` 独立控制本地任务提交 API；新安装默认只开启前者。旧 `native_api` 配置仍可读取，保留原有开关行为，但不能与新字段混写。

进一步参考：[Provider 用法](docs/PROVIDER.md) · [微信适配](docs/WECHAT_LINUX.md) · [代码地图](src/wxbridge/README.md) · [实例配置示例](examples/instance/config.example.jsonc) · [开发约定](CONTRIBUTING.md) · [安全与隐私](SECURITY.md)。

后端开发检查入口为 `python -X utf8 scripts/test-offline.py` 和 `python scripts/check-repository.py`；前端检查使用 `npm --prefix src/web/frontend test` 和 `npm --prefix src/web/frontend run build`。离线测试不调用真实模型、不发送微信消息。

许可证暂未选定，目前用于受邀协作。密钥、私有配置、账号数据、聊天记录和记忆不随源码发布。
