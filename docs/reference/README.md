# 历史参考资料

当前入口：[provider 用法](../PROVIDER.md)、[wechat_linux 状态与调用列表](../WECHAT_LINUX.md)。

本目录保留仍有维护价值的函数定位、回调生命周期、验证证据、故障原因和运维约束。它不是当前接口承诺；旧示例、状态与测试数量只描述原有时间点。归档没有重新执行这些实验。

## 微信分析与验证

- [完整研究报告](WECHAT_RESEARCH_REPORT.md)：数据库、消息和原生调用研究。
- [离线分析方法](WECHAT_ANALYSIS.md)：版本比较、指纹候选和反汇编。
- [文字发送分析](NATIVE_ANALYSIS.md)：对象构造、后台服务和版本约束。
- [原生图片下载分析](NATIVE_IMAGE_ANALYSIS.md)：协程、future 和回调生命周期。
- [图片下载验证 JSON](research/native-image-validation.json)：保留的脱敏实验结果。

## 运维与故障

- [部署记录](DEPLOYMENT.md)、[开机恢复](BOOT_RECOVERY.md)：原有专用用户、内存挂载和服务关系。
- [本地 API 记录](NATIVE_API.md)：上层队列、协议和发送确认的历史约定。
- [审阅修复](REVIEW_FIXES.md)、[架构调整](ARCHITECTURE_REFACTOR.md)。
- [附件索引缺失故障](incidents/image-index-20261025.md)：来源绑定与缓存回退边界。

相关示意图保留在 images/。重复 HTML 报告和旧业务使用文档不再维护，需要追溯时可从 Git 历史获取。
