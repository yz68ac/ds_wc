# 独立群实例配置

根 config.json 仅保存账号、数据库、微信发送和运行路径等共享资源。启动时扫描其同级 instance/*/config.json；目录名即实例 ID，文件内容为平铺的群配置，不使用根 instances 列表或 overrides。

```text
config.json
instance/
  laidahui/
    config.json
    system.md
  another-group/
    config.json
    system.md
```

每份群配置填写 chat（稳定 xxx@chatroom ID）和 enabled；可分别指定 model_clients、models、system_prompt_file、target、queue、memory、search、vision、proactive。没有填写的业务字段继承 config.defaults.json。各群配置互不继承。

system_prompt_file 及显式指定的 memory.system_prompt_file 相对该实例目录解析。provider_env、微信资源及基础队列/记忆路径由账号配置提供；群配置不能覆盖这些共享资源。实例的队列和状态自动放在基础运行目录 instances/<id>/ 下；记忆继续按账号和群身份隔离。

只有 enabled=true 的实例创建 GroupSession；停用实例允许 chat 留空以便准备配置。启用前必须填写合法且唯一的群 ID 和所需模型，发送允许清单也须包含此群。没有启用实例时，run 明确报告 no_enabled_instances，诊断配置命令仍可使用。

每群拥有 reply/image/background 三条独立模型通道，共享 provider 连接定义但不共享 client。微信账号发送门禁及未确认发送核对保持集中管理。配置变化需重启。

使用 python -m wxbridge --instance laidahui explain-config 查看最终配置；实例 ID 对应目录名。实际 instance/ 目录已被 Git 忽略。公开模板位于 [examples/instance](../examples/instance/README.md)。
