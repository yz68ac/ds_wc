"""Offline, read-only tools for studying version-specific WeChat ELF samples.

No import in this package attaches to a client, executes samples, reads account
data, updates profiles or creates services. A static match is not runtime proof.
"""

from wechat_linux.analysis.elf import ELFError, ELFFile
from wechat_linux.analysis.audit import (ProfileError, audit_image, compare_images,
                                          find_fingerprint, load_profile, load_profile_file)
from wechat_linux.analysis.disassembly import DisassemblyError, disassemble

__all__ = ['ELFError', 'ELFFile', 'ProfileError', 'load_profile', 'load_profile_file',
           'audit_image', 'compare_images', 'find_fingerprint', 'DisassemblyError', 'disassemble']
