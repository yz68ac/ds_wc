# 参与开发

先让离线测试通过，再接入自己的测试账号。Windows 可以编辑和跑离线回归；正式运行依赖 Linux 原生微信，现有启动、登录和恢复保留虚拟显示环境，见 [部署说明](docs/DEPLOYMENT.md)。项目许可证暂未选定，目前用于受邀协作。

## 准备开发环境

需要 Git、Python 3.13 或 3.14。在仓库根目录创建 `.venv`，安装 `requirements-test.txt`，然后运行 `scripts/test-offline.py`。具体命令见 [README](README.md)。测试使用合成数据，不需要微信登录、API Key 或服务器权限。

建议每个改动使用独立分支：

```bash
git switch main
git pull --ff-only
git switch -c feat/your-change
# 修改、检查后，按文件暂存，避免误带本机数据
git add path/to/changed-file
git commit -m "Describe the resulting behavior"
git push -u origin feat/your-change
```

提交 PR 时说明具体问题、修改后的行为、验证结果，以及是否涉及配置或数据格式迁移。给修复行为补有意义的合成测试；纯文档和低影响调整无需机械增加测试。

## 模块与约束

- `app` 负责调度，主进程拥有所有微信数据库查询；API 和原生/UI发送放入受控工作进程。前台优先，有效 API 并发遵守现有限额。
- `ai` 处理模型输入输出。群系统提示词由配置提供，不写入固定人设或试用风格；不要将模型内部推理、群聊正文或凭据写入日志。
- `wechat` 适配原生客户端。真实 @ 要核对消息元数据，不能仅匹配正文中的昵称。
- `native` 负责本机发送接口与固定版本内部调用。项目源码不被特权helper直接导入；修改helper、agent或profile后，必须成套核对安装资产，不能自动放宽版本指纹。
- `storage` 管理队列、记忆与来源校验。账号、群、稳定成员 ID 必须隔离；记忆建议经过主进程验证后才能落盘。
- 发送前持久化意图、校验收件人和允许清单；桌面后端还需校验群和草稿。提交后由主进程确认新增本人发出记录。状态不明不能盲目重发、回退另一后端，也不能把函数或按钮返回当作发送成功。
- 解密副本和运行队列必须在通过校验的私有 `tmpfs,noswap`；维护修改与监听共用单实例锁。

新增配置时一并更新 `config.defaults.json`、`config.example.jsonc`、`src/wxbridge/config.py` 的校验及 [配置文档](docs/CONFIG.md)。实际 `config.json`、`.env`、`memory/` 属于部署数据，不应提交。

完整链路和文件职责见 [代码导读](docs/CODE_GUIDE.md)、[native地图](src/wxbridge/native/README.md) 和 [脚本分类](scripts/README.md)。不要为方便测试绕过上述校验。

## 提交前检查

以下 `python` 指当前虚拟环境中的解释器：

```bash
python -X utf8 scripts/test-offline.py
python scripts/check-repository.py --history
git diff --check
git diff --cached --stat
```

检查暂存内容中没有真实账号 ID、聊天、截图、数据库、密钥或记忆。静态检查只能减少误提交风险，不能代替人工检查。诊断材料按 [安全说明](SECURITY.md) 脱敏。

GitHub Actions 运行 Windows/Linux、Python 3.13/3.14 离线回归，不访问真实微信、不使用 API Key，也不自动部署。合并代码后，Linux 上线仍需备份、停止监听、保留实际配置/凭据/人格/记忆/游标，再验证恢复与收发。
