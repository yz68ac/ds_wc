"""Offline profile diagnostics; never attaches to or executes a client."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import sys


def _rva(value):
    try:
        number = int(value, 0)
    except ValueError:
        raise argparse.ArgumentTypeError('请输入十六进制地址（如 0x6517dc0）或十进制整数') from None
    if not 0 <= number <= (1 << 64)-1:
        raise argparse.ArgumentTypeError('RVA 超出范围')
    return number


def _parser():
    parser = argparse.ArgumentParser(prog='python -m wechat_linux',
        description='WeChat Linux 离线分析工具：只读文件，不连接微信进程')
    root = parser.add_subparsers(dest='area', required=True)
    area = root.add_parser('analysis', help='检查 ELF、版本指纹与原生适配资产')
    commands = area.add_subparsers(dest='command', required=True)

    def command(name, help_text):
        sub = commands.add_parser(name, help=help_text, description=help_text)
        sub.add_argument('--json', action='store_true', help='输出完整、可解析的 JSON')
        return sub

    def profile(sub, *, include_all=False):
        group = sub.add_mutually_exclusive_group()
        choices = ('text', 'image', 'all') if include_all else ('text', 'image')
        group.add_argument('--profile', choices=choices, help='选择库内版本档案')
        group.add_argument('--profile-file', type=Path, help='显式读取自定义 JSON 档案')

    command('profiles', '列出内置版本档案及指纹数量')
    inspect = command('inspect', '查看 ELF 格式、架构、SHA-256 和可加载段')
    inspect.add_argument('binary', type=Path)
    check = command('check', '核对完整 SHA-256、架构及所有函数指纹')
    check.add_argument('binary', type=Path)
    profile(check, include_all=True)
    compare = command('compare', '比较旧、新 ELF 在旧档案 RVA 处的指纹')
    compare.add_argument('reference', type=Path)
    compare.add_argument('candidate', type=Path)
    profile(compare)
    find = command('find', '在候选 ELF 的可执行段搜索一个已知指纹')
    find.add_argument('binary', type=Path)
    find.add_argument('--rva', type=_rva, required=True, help='旧档案中的函数 RVA')
    find.add_argument('--limit', type=int, default=10, help='最多列出 1–100 个候选，默认 10')
    find.add_argument('--reference', type=Path, help='匹配旧档案的参考 ELF；与 --pattern-bytes 同时使用')
    find.add_argument('--pattern-bytes', type=int, help='从验证过的参考函数起点读取的字节数')
    profile(find)
    inv = command('inventory', '盘点内置 JS 中直接写出的地址及档案字段')
    inv.add_argument('--profile', choices=('text', 'image', 'all'), default='all')
    disasm = command('disasm', '使用可选 Capstone 查看给定 RVA 附近的汇编')
    disasm.add_argument('binary', type=Path)
    disasm.add_argument('--rva', type=_rva, required=True)
    disasm.add_argument('--bytes', type=int, default=64, dest='size', help='读取字节数，1–4096')
    disasm.add_argument('--count', type=int, default=16, help='最多输出指令数，1–256')
    return parser


def _profile(args, *, default='image'):
    from wechat_linux.analysis.audit import load_profile, load_profile_file
    return load_profile_file(args.profile_file) if args.profile_file else load_profile(args.profile or default)


def _inspect(elf, filename):
    segments = [dict(index=s.index, file_offset=hex(s.offset), rva=hex(s.vaddr),
                     file_bytes=s.filesz, memory_bytes=s.memsz, alignment=s.align,
                     permissions=''.join(c for c, present in (('R', s.readable), ('W', s.writable),
                                                              ('X', s.executable)) if present))
                for s in elf.segments]
    result = dict(schema_version=1, operation='inspect', name=Path(filename).name,
                  size=elf.size, sha256=elf.sha256(), elf_type=elf.elf_type,
                  architecture=elf.architecture, image_vaddr=hex(elf.image_vaddr), segments=segments,
                  notice='Static ELF metadata only; it does not prove this client can be invoked safely.')
    elf.check_unchanged()
    return result


def _render(command, value, as_json):
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return
    if command == 'profiles':
        for item in value['profiles']:
            print(f"{item['name']}: {item['version']} / {item['arch']} / {item['backend']} / {item['fingerprints']} 个指纹")
            print('  SHA-256: '+item['sha256'])
    elif command == 'inspect':
        print(f"文件: {value['name']}；{value['elf_type']} / {value['architecture']}；{value['size']} 字节")
        print('SHA-256: '+value['sha256'])
        print('最低映射虚拟地址: '+value['image_vaddr'])
        for item in value['segments']:
            print(f"  段 {item['index']}: RVA {item['rva']}, 文件偏移 {item['file_offset']}, "
                  f"文件 {item['file_bytes']} 字节, 内存 {item['memory_bytes']} 字节, {item['permissions']}")
    elif command == 'check':
        for result in value['profiles']:
            checks = result['checks']
            passed = sum(row['status'] == 'match' for row in result['fingerprints'])
            print(f"{result['profile']['name']}: {'静态匹配' if result['static_match'] else '静态不匹配'}；"
                  f"指纹 {passed}/{len(result['fingerprints'])}；SHA-256 {'匹配' if checks['sha256_match'] else '不匹配'}；"
                  f"架构 {'匹配' if checks['architecture_match'] else '不匹配'}")
            for row in result['fingerprints']:
                if row['status'] != 'match':
                    print(f"  {row['rva']}: {row['status']}" + (f" ({row['error']})" if row.get('error') else ''))
    elif command == 'compare':
        print(f"参考样本匹配档案: {value['reference_matches_profile']}；候选样本匹配档案: {value['candidate_matches_profile']}")
        print('两文件 SHA-256 相同: '+str(value['same_sha256']))
        for item in value['fingerprints']:
            print(f"  {item['rva']}: 原样本 {item['reference']['status']}，新样本 {item['candidate']['status']}，"
                  f"同 RVA 字节相同 {item['same_bytes_at_rva']}")
    elif command == 'find':
        print(f"旧指纹 RVA: {value['source_rva']}；状态: {value['status']}；候选数: {len(value['matches'])}")
        if value.get('anchor_length'):
            print(f"锚点来源: {value['anchor_source']}；长度: {value['anchor_length']} 字节")
        for item in value['matches']:
            print(f"  候选 RVA {item['rva']}，文件偏移 {item['file_offset']}")
        print(f"候选唯一: {value['unique_candidate']}；结果截断: {value['truncated']}；"
              f"映射不明确: {value['unresolved_mappings']}")
        if value.get('anchor_source') == 'verified_reference_bytes':
            print('长锚点可能受跨版本重定位或相对跳转影响，未找到也不能证明函数不存在。')
    elif command == 'disasm':
        print(f"{value['architecture']} 从 RVA {value['start_rva']} 开始，读取 {value['examined_bytes']} 字节")
        for item in value['instructions']:
            print(f"  {item['rva']:<14} {item['bytes']:<32} {item['mnemonic']:<10} {item['operands']}")
    elif command == 'inventory':
        for name, item in value['backends'].items():
            summary = item['summary']
            print(f"{name}: {summary['function_fingerprints']} 个档案指纹；"
                  f"{summary['literal_rvas']} 处直接地址表达式；"
                  f"{summary['unresolved_expressions']} 处动态或未识别表达式")
            for entry in item['module_expressions']:
                if entry['kind'] == 'literal_module_rva' and not entry['has_function_fingerprint']:
                    print(f"  JS 第 {entry['line']} 行: {entry['hex']}（未列为函数指纹；可能是数据地址）")
        print('说明：只做有限文本扫描，结果可能包含数据或漏掉间接引用；详细结果用 --json。')
    else:
        print(json.dumps(value, ensure_ascii=False, indent=2))
    if command in ('check', 'compare', 'find', 'disasm'):
        print('注意：静态结果只提供分析线索，不证明运行时 ABI、对象生命周期或调用安全。')


def main(argv=None):
    args = _parser().parse_args(argv)
    from wechat_linux.analysis.audit import (ProfileError, audit_image, compare_images,
                                              find_fingerprint, load_profile)
    from wechat_linux.analysis.disassembly import DisassemblyError, disassemble
    from wechat_linux.analysis.elf import ELFError, ELFFile
    from wechat_linux.analysis.inventory import inventory
    code = 0
    try:
        with ExitStack() as stack:
            if args.command == 'profiles':
                value = dict(schema_version=1, operation='profiles',
                             profiles=[load_profile(name).summary() for name in ('text', 'image')])
            elif args.command == 'inventory':
                value = inventory(None if args.profile == 'all' else args.profile)
            elif args.command == 'inspect':
                value = _inspect(stack.enter_context(ELFFile(args.binary)), args.binary)
            elif args.command == 'check':
                elf = stack.enter_context(ELFFile(args.binary))
                profiles = ([load_profile(name) for name in ('text', 'image')]
                            if not args.profile_file and args.profile in (None, 'all') else [_profile(args)])
                results = [audit_image(elf, profile) for profile in profiles]
                value = dict(schema_version=1, operation='check', profiles=results)
                code = 0 if all(result['static_match'] for result in results) else 1
            elif args.command == 'compare':
                reference = stack.enter_context(ELFFile(args.reference))
                candidate = stack.enter_context(ELFFile(args.candidate))
                value = compare_images(reference, candidate, _profile(args))
            elif args.command == 'find':
                elf = stack.enter_context(ELFFile(args.binary))
                if (args.reference is None) != (args.pattern_bytes is None):
                    raise ProfileError('reference_and_pattern_bytes_required')
                reference = stack.enter_context(ELFFile(args.reference)) if args.reference else None
                value = find_fingerprint(elf, _profile(args), args.rva, limit=args.limit,
                                         reference=reference, pattern_bytes=args.pattern_bytes)
                code = 0 if value['status'] == 'candidate_search' else 1
            else:
                elf = stack.enter_context(ELFFile(args.binary))
                value = disassemble(elf, args.rva, size=args.size, count=args.count)
        _render(args.command, value, args.json)
        return code
    except (ProfileError, ELFError, DisassemblyError, OSError, ValueError) as exc:
        code_text = getattr(exc, 'code', None) or (str(exc) if isinstance(exc, DisassemblyError) else 'file_or_analysis_error')
        if args.json:
            print(json.dumps(dict(error=code_text), ensure_ascii=False), file=sys.stderr)
        else:
            message = '（可选依赖：python -m pip install ".[analysis]"）' if code_text == 'capstone_not_installed' else ''
            print('分析失败: '+str(code_text)+message, file=sys.stderr)
        return 2
