# 微信 Linux 版本与二进制离线分析

`wechat_linux.analysis` 是学习与升级时的只读工具。它查看用户明确提供的 ELF、库内版本档案及随库的 JS/C 资源；不会连接或启动微信，不读取聊天、账号或密钥，不调用 Frida，也不生成、修改或替换生产 profile。

静态匹配只说明文件 hash、架构与档案中登记的函数字节一致，**不证明实际调用安全**。新版即使找到唯一字节候选，也仍须人工检查调用关系、参数与返回值、C++ 对象、线程/协程和资源生命周期，并在独立测试环境验证。

## 一次升级时怎么用

以下示例以项目根目录为当前目录，`old-wechat` 和 `new-wechat` 分别替换成已解包的旧、新 ELF 文件。Windows 使用 `python`；Linux 可将其替换为 `python3`。微信二进制需由使用者自行提供，不随 wheel 发行。

1. 先检查库支持哪些参考档案，以及样本的基本信息：

   ```bash
   python -m wechat_linux analysis profiles
   python -m wechat_linux analysis inspect old-wechat
   python -m wechat_linux analysis inspect new-wechat
   ```

   `inspect` 输出 ELF 类型、架构、SHA-256、可加载段的 RVA、文件偏移、长度和权限，不从 ELF 猜测产品版本号。分析器当前只解析小端 ELF64；RVA 操作要求最低 PT_LOAD 虚拟地址为 0，其他布局明确拒绝推测。

2. 核对旧样本确实对应现有档案：

   ```bash
   python -m wechat_linux analysis check old-wechat --profile image
   python -m wechat_linux analysis check old-wechat --profile text
   ```

   未指定 `--profile` 时同时检查 text 和 image；只有所选档案全部静态匹配才返回退出码 0，否则返回 1。输入/分析错误返回 2。用 `--json` 可取得每项检查、指纹和错误代码，供脚本处理。

3. 对照新旧文件中的旧地址：

   ```bash
   python -m wechat_linux analysis compare old-wechat new-wechat --profile image
   ```

   参考文件不匹配档案时，报告会明确显示 `reference_matches_profile=false`，此时应先找正确的旧样本。新样本不同是比较操作的正常结果；比较成功本身返回 0，不等于新版可调用。

4. 从新文件寻找一个旧指纹作为线索：

   ```bash
   python -m wechat_linux analysis find new-wechat --profile image --rva 0x6517dc0
   ```

   工具仅搜索该档案已登记的单个指纹，范围限于文件内可执行 PT_LOAD 段，默认最多展示 10 个候选。`truncated=true` 或映射不明确时不会报告唯一候选。扫描设置上限，达到上限会明确报告不完整；没有找到不代表函数不存在。

   常见函数开头可能重复。可以显式提供已通过完整档案校验的旧 ELF，从该函数起点取更长的字节作为研究锚点：

   ```bash
   python -m wechat_linux analysis find new-wechat --profile image --rva 0x6517dc0 --reference old-wechat --pattern-bytes 64
   ```

   `--reference` 与 `--pattern-bytes` 必须同时提供；旧文件必须与完整档案静态匹配。长锚点仍不能证明候选正确，而且编译、重定位或相对跳转变化都可能让相同逻辑的字节发生变化。所有结果均须人工复核。

5. 如需查看候选处的汇编，可使用可选 Capstone：

   ```bash
   python -m pip install '.[analysis]'
   python -m wechat_linux analysis disasm new-wechat --rva 0x6517dc0 --bytes 64 --count 16
   ```

   工具只读取给定 RVA 后的有限字节，指令地址仍用 RVA。Capstone 没有证明这里一定是函数起点；应与 Ghidra 的函数边界、调用点及真实运行证据交叉检查。基础 inspect/check/compare/find/inventory 功能只需 Python 标准库。

当前仓库有一个本地备用样本，可以用下列命令尝试，不需要启动微信：

```bash
python -m wechat_linux analysis check resources/wechat/linux-x86_64/4.1.13.26/binary/wechat
python -m wechat_linux analysis disasm resources/wechat/linux-x86_64/4.1.13.26/binary/wechat --rva 0x6517dc0 --bytes 16
```

该资源可能未随仓库克隆或安装包一起提供；如果不存在，请显式提供自己的合法 ELF 文件。

## profile 和源代码中还有哪些地址

```bash
python -m wechat_linux analysis inventory --profile image
python -m wechat_linux analysis inventory --profile text --json
```

此命令只盘点库内资源：档案登记了哪些函数指纹、JS 中直接出现了哪些 `base.add(0x...)` / `native(0x...)` 表达式、引用了哪些 `PROFILE` 字段。未列为函数指纹的字面地址可能是数据、虚表或其他常量，不能一律当作函数。它是有限的文本分析，不执行 JS/C，也不是完整语法分析器；间接引用、动态计算和 C++ 对象布局可能遗漏。

文件 RVA 与文件偏移不是同一个概念。例如当前研究样本的可执行段，虚拟地址与文件偏移相差 `0x1000`；工具依据 PT_LOAD 段转换，并拒绝跨段、仅存在于 BSS 的范围及有歧义的映射。不能直接把 RVA 当文件偏移做 `seek`。

需要研究自定义档案时，可以用 `--profile-file your-profile.json` 显式读取文件，代替 `--profile`。自定义档案只用于本次静态分析；它不会覆盖内置档案、启用 helper 或绕过运行时检查。档案包含函数指纹，不等于完整的对象布局、服务定位逻辑或调用契约。

## 作为 Python 库使用

```python
from wechat_linux.analysis import ELFFile, load_profile, audit_image, compare_images, find_fingerprint

profile = load_profile('image')
with ELFFile('old-wechat') as reference, ELFFile('new-wechat') as candidate:
    old_check = audit_image(reference, profile)
    comparison = compare_images(reference, candidate, profile)
    candidates = find_fingerprint(candidate, profile, 0x6517dc0,
                                  reference=reference, pattern_bytes=64)
```

返回值均为 JSON 可序列化的普通数据，使用 `static_match` 表示严格的静态结果；`runtime_compatible` 保留为 `null`。文件通过单个只读描述符访问并检测检查期间的文件变化；这不是对恶意修改者伪造所有文件元数据的绝对保证。

更完整的原版研究步骤、汇编与调用证据见[微信研究报告](WECHAT_RESEARCH_REPORT.md)和[原生图片分析](NATIVE_IMAGE_ANALYSIS.md)。
