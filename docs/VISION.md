# 引用图片并提问

在目标群里引用一条图片消息，从成员列表真正 @ 助手，再问“这怎么看”“图里写了什么”等问题。当前只带入这次提问明确引用的图片，不把十分钟历史中的其他图片自动上传。每次 @ 独立排队，连续 @ 合并仍关闭。

## 图片如何到达模型

1. 主进程解析引用中的图片标识。快照不完整时，用原消息 `server_id` 在当前群查询图片记录；不查询其他群的消息。
2. 主进程读取本账号 `hardlink.db` 附件索引，把消息 MD5 映射为实际的本地缓存文件名。图片 MD5 通常不等于文件名，不能直接猜路径。部分图片的 XML MD5 未入索引，此时仅在原消息的群、server_id、作者和图片类型核验通过后，解析原消息 `packed_info_data` 的 protobuf 3→4 字段取得本地缓存名，再按该文件名查询同一附件索引。不会根据时间或“最近一张图片”猜测，不使用引用快照自带的文件名。
3. API 工作进程只读本地加密文件，依次尝试 `_h.dat` 原图、普通 `.dat` 微信缓存图、预览图和缩略图。普通缓存图不保证是原始上传图。已验证当前 Linux 微信的 V2 格式：由本账号 kvcomm 缓存标识推导 AES/XOR 参数，在内存解密并检查文件格式、完整性标记、尺寸和大小；兼容 V1 和旧 XOR 格式。
4. 解密后的 JPEG/PNG/GIF/WebP 作为用户消息的 `image_url` 内联 base64 数据发送到当前配置 API。没有单独上传到 Files API，不下载消息 XML 中的 CDN URL，也不把 CDN 密钥提供给模型。
5. 提示词明确关联问题、图片编号、引用作者和原时间。使用缩略图或预览图时明确标记质量，提醒小字不可辨时不要补造。API 生成、发送队列、冷却和发出记录确认沿用现有流程。

微信原始缓存仍由微信管理；本程序不保存解密图片文件、图片 base64 或图片密钥。队列只保存引用标识及安全结果元数据，日志不写图片/聊天正文。调用 API 时图片会发送到配置的服务端，与文本提问使用同一账户和计费渠道。

若本地没有图片、附件索引缺失、格式无法解析或超出限制，会明确标为不可读，请求模型说明没有看到图片并让用户重新发送/打开原图后再引用。不会把猜测描述为识别成功。

wxgf 已支持：`ai/wxgf.py` 提取完整 HEVC 参数集和图像流，通过 SPS 色度字段区分彩色流与单色遮罩，优先选彩色流，避免直接取第一帧得到空白遮罩。使用 Linux 已有的 libde265、libswscale、libavutil 公开 C 接口，在独立 Python 子进程内转成原尺寸 PNG，经原有校验后再传给模型；不额外安装 FFmpeg、不保存明文图片，也不把预览图放大冒充原图。当前只取所选流的第一帧，不提供动画播放或透明遮罩合成。

转码子进程仅继承 PATH/LANG，不继承 API Key 等业务环境；管道输入输出，墙钟 8 秒、CPU 5 秒、地址空间 768 MiB，禁用 core dump 和文件输出，输入/输出最多 8 MiB，图像单边 8192、最多 16777216 像素。原缓存不修改，缺库、超时、损坏或超限时回退可读预览/缩略图。已确认容器后的转码错误不会再被后续错误密钥覆盖。`source_format=wxgf`、`converted=true` 与 `fallback_errors` 只记录格式/转换/失败码，不含密钥、路径或图像。

Linux 运行依赖为系统库 libde265、libswscale、libavutil（当前 Debian 包 libde265-0、libswscale8、libavutil59 已存在）；没有新增 Python 依赖。新 wxgf 测试包含合成单色遮罩和彩色 HEVC 图，验证选中正确流、保留分辨率与颜色，以及超时/缺库等回退行为。

## 配置与状态

| `vision` 配置 | 默认值 | 作用 |
| --- | --- | --- |
| `enabled` | `true` | 开启明确引用图片的本地读取及传图；关闭后保留文字引用行为 |
| `detail` | `original` | API 图像细节，可选 original/high/low/auto；不能恢复缩略图丢失的细节 |
| `max_images` | `4` | 单请求最多 1–4 张图片 |
| `max_image_bytes` | `8388608` | 每张解密图片上限，最多 8 MiB |
| `cache_wait_seconds` | `3` | 已定位文件尚未就绪时，在工作进程内最多等待的秒数，允许 0–10 |

图片单边最多 8192 像素；整个 JSON 请求限制 48 MiB。限制前检查，不在错误信息中打印请求。修改 JSON 后需要重启机器人。

转码实现依据公开接口：[libde265](https://github.com/strukturag/libde265/blob/master/libde265/de265.h)、[FFmpeg libswscale](https://github.com/FFmpeg/FFmpeg/blob/n7.1/libswscale/swscale.h)；容器分流参考作者的 [wxgf 格式分析](https://www.harvey.eu.org/posts/wxzm)，使用自有样本验证。未复制第三方解码程序或随项目分发共享库。

`queue-status` / `live-status.json` 的 `last_vision` 记录最近图片任务、实际附送张数、引用数和各图的格式/尺寸/质量或错误码；不包含账号、本地路径、图片内容或密钥。`images=0` 表示没有实际附送图片，即使生成了文本回复也不能算图片识别成功。

离线检查：`python3 scripts/test-offline.py`。显式 API 检查：`python3 scripts/verify-vision-api.py --synthetic-api`，用合成红/蓝色块核对当前模型能否观察图片，不读取微信、不发送群消息，但会产生少量 API 费用。

当前模型 `deepseek-flash` 支持图片输入，仍走 `/chat/completions`。官方能力与格式依据：[DeepSeek 图像理解](https://api-docs.deepseek.com/zh-cn/guides/vision/)（2026-10-20 核对）。
