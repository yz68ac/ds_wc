"""Offline disassembly with optional Capstone; no real process attachment."""
import importlib.util
import sys
import unittest
from unittest.mock import Mock, patch

from wechat_linux.analysis.disassembly import DisassemblyError, disassemble


class DisassemblyTests(unittest.TestCase):
    def setUp(self):
        self.elf = Mock(architecture='x86_64')
        self.elf.read_rva.return_value = bytes.fromhex('554889e5c3')

    @unittest.skipUnless(importlib.util.find_spec('capstone'), 'optional Capstone not installed')
    def test_decodes_requested_rva_without_executing_input(self):
        result = disassemble(self.elf, 0x1200, size=5, count=4)
        self.elf.read_rva.assert_called_once_with(0x1200, 5, executable=True)
        self.elf.check_unchanged.assert_called_once_with()
        self.assertEqual([(row['rva'], row['mnemonic']) for row in result['instructions']],
                         [('0x1200', 'push'), ('0x1201', 'mov'), ('0x1204', 'ret')])
        self.assertIn('do not prove', result['limitation'])

    def test_limits_and_architecture_checked_before_read(self):
        for kwargs in (dict(rva=-1), dict(rva=True), dict(rva=0, size=0),
                       dict(rva=0, size=4097), dict(rva=0, count=0), dict(rva=0, count=257)):
            with self.subTest(kwargs=kwargs), self.assertRaises(DisassemblyError):
                disassemble(self.elf, **kwargs)
        self.elf.architecture = 'aarch64'
        with self.assertRaisesRegex(DisassemblyError, 'disassembly_arch_unsupported'):
            disassemble(self.elf, 0)
        self.elf.read_rva.assert_not_called()

    def test_missing_optional_dependency_is_actionable_and_does_not_read_binary(self):
        with patch.dict(sys.modules, {'capstone': None}):
            with self.assertRaisesRegex(DisassemblyError, 'capstone_not_installed'):
                disassemble(self.elf, 0)
        self.elf.read_rva.assert_not_called()
