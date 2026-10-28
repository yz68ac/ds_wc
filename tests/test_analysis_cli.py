"""CLI checks synthetic ELF samples only; no native client, networking or credentials."""
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest

from wechat_linux.analysis.cli import main


def synthetic_elf():
    data = bytearray(4096)
    ident = b'\x7fELF' + bytes([2, 1, 1, 0]) + bytes(8)
    header = struct.pack('<16sHHIQQQIHHHHHH', ident, 3, 62, 1, 0x100, 64, 0, 0,
                         64, 56, 1, 64, 0, 0)
    segment = struct.pack('<IIQQQQQQ', 1, 5, 0, 0, 0, len(data), len(data), 4096)
    data[:len(header)] = header
    data[64:64+len(segment)] = segment
    data[0x100:0x110] = bytes.fromhex('554157415641554154534881ec880400')
    return bytes(data)


class AnalysisCLITests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1]/'.test-tmp'
        root.mkdir(exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        data = synthetic_elf()
        self.old = self.root/'old.elf'
        self.old.write_bytes(data)
        candidate = bytearray(data)
        candidate[0x200:0x210] = candidate[0x100:0x110]
        candidate[0x100:0x110] = bytes(16)
        self.new = self.root/'new.elf'
        self.new.write_bytes(candidate)
        self.profile = self.root/'profile.json'
        self.profile.write_text(json.dumps(dict(version='test', arch='x86_64', backend='synthetic',
            sha256=hashlib.sha256(data).hexdigest(), functions={'0x100': data[0x100:0x110].hex()})), encoding='utf-8')

    def call(self, argv):
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            code = main(['analysis', *argv])
        return code, json.loads(output.getvalue()) if output.getvalue() else None, error.getvalue()

    def test_inspect_and_exact_profile_match(self):
        before = self.old.read_bytes()
        code, info, _ = self.call(['inspect', '--json', str(self.old)])
        self.assertEqual(code, 0)
        self.assertEqual(info['architecture'], 'x86_64')
        self.assertEqual(info['segments'][0]['permissions'], 'RX')
        code, result, _ = self.call(['check', '--json', '--profile-file', str(self.profile), str(self.old)])
        self.assertEqual(code, 0)
        self.assertTrue(result['profiles'][0]['static_match'])
        self.assertIsNone(result['profiles'][0]['runtime_compatible'])
        self.assertEqual(self.old.read_bytes(), before)

    def test_changed_sample_returns_mismatch_and_compare_explains(self):
        code, result, _ = self.call(['check', '--json', '--profile-file', str(self.profile), str(self.new)])
        self.assertEqual(code, 1)
        self.assertFalse(result['profiles'][0]['static_match'])
        code, comparison, _ = self.call(['compare', '--json', '--profile-file', str(self.profile),
                                        str(self.old), str(self.new)])
        self.assertEqual(code, 0)
        self.assertTrue(comparison['reference_matches_profile'])
        self.assertFalse(comparison['candidate_matches_profile'])
        self.assertFalse(comparison['fingerprints'][0]['same_bytes_at_rva'])
        self.assertIsNone(comparison['runtime_compatible'])

    def test_find_reports_candidate_without_promoting_it_to_compatibility(self):
        code, result, _ = self.call(['find', '--json', '--profile-file', str(self.profile),
                                    '--rva', '0x100', str(self.new)])
        self.assertEqual(code, 0)
        self.assertEqual(result['matches'][0]['rva'], '0x200')
        self.assertTrue(result['unique_candidate'])
        self.assertIsNone(result['runtime_compatible'])
        self.assertEqual(json.loads(self.profile.read_text())['functions']['0x100'], synthetic_elf()[0x100:0x110].hex())

    def test_expanded_anchor_requires_matching_reference_and_both_options(self):
        code, result, _ = self.call(['find', '--json', '--profile-file', str(self.profile), '--rva', '0x100',
                                     '--reference', str(self.old), '--pattern-bytes', '32', str(self.new)])
        self.assertEqual(code, 0)
        self.assertEqual(result['anchor_source'], 'verified_reference_bytes')
        self.assertEqual(result['anchor_length'], 32)
        self.assertEqual(result['matches'][0]['rva'], '0x200')
        code, _, error = self.call(['find', '--json', '--profile-file', str(self.profile), '--rva', '0x100',
                                    '--reference', str(self.old), str(self.new)])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(error)['error'], 'reference_and_pattern_bytes_required')
        code, _, error = self.call(['find', '--json', '--profile-file', str(self.profile), '--rva', '0x100',
                                    '--reference', str(self.new), '--pattern-bytes', '32', str(self.new)])
        self.assertEqual(code, 2)
        self.assertIn('error', json.loads(error))

    def test_invalid_file_exits_safely_without_traceback(self):
        self.old.write_bytes(b'not an ELF')
        code, value, error = self.call(['inspect', '--json', str(self.old)])
        self.assertEqual(code, 2)
        self.assertIsNone(value)
        self.assertIn('error', json.loads(error))
        self.assertNotIn('Traceback', error)
