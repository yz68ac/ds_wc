"""Optional bounded disassembly for offline learning; no executable mapping."""
from __future__ import annotations


class DisassemblyError(ValueError):
    pass


def disassemble(elf, rva, *, size=64, count=16):
    """Disassemble one file-backed executable range, reporting module-relative RVAs.

    Capstone is optional and imported only when requested. Decoded instructions
    depend on the supplied starting address; they do not establish function
    boundaries, signatures or compatibility.
    """
    if type(rva) is not int or rva < 0:
        raise DisassemblyError('invalid_rva')
    if type(size) is not int or not 1 <= size <= 4096:
        raise DisassemblyError('invalid_disassembly_size')
    if type(count) is not int or not 1 <= count <= 256:
        raise DisassemblyError('invalid_instruction_count')
    if elf.architecture != 'x86_64':
        raise DisassemblyError('disassembly_arch_unsupported')
    try:
        import capstone
    except ImportError:
        raise DisassemblyError('capstone_not_installed') from None
    data = elf.read_rva(rva, size, executable=True)
    engine = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    instructions = [dict(rva=hex(item.address), bytes=bytes(item.bytes).hex(),
                         mnemonic=item.mnemonic, operands=item.op_str)
                    for item in engine.disasm(data, rva, count=count)]
    elf.check_unchanged()
    return dict(architecture=elf.architecture, start_rva=hex(rva), examined_bytes=len(data),
                instructions=instructions,
                limitation='Starting address and decoded instructions do not prove function boundaries or runtime compatibility.')
