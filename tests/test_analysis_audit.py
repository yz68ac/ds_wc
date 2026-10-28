"""Bounded offline profile checks on synthetic images; never attach or load a client."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from wechat_linux.analysis.audit import (Fingerprint, ProfileError, audit_image, compare_images,
    find_fingerprint, load_profile, load_profile_file, normalize_profile)
from wechat_linux.analysis.elf import ELFError, ELFFile

PATTERN = bytes.fromhex('554157415641554154534881ec880400')


class SyntheticELF:
    """Minimal file-backed executable fixture for audit protocol testing."""
    def __init__(self, data, *, arch='x86_64', image_vaddr=0, ambiguous=()):
        self.data = bytes(data)
        self.architecture, self.image_vaddr, self.elf_type = arch, image_vaddr, 'ET_DYN'
        self.size, self.ambiguous = len(data), set(ambiguous)

    def sha256(self):
        self.check_unchanged()
        return hashlib.sha256(self.data).hexdigest()

    def check_unchanged(self):
        return None

    def rva_to_offset(self, rva, size=1, *, executable=True):
        if self.image_vaddr:
            raise ELFError('nonzero_image_vaddr')
        if rva in self.ambiguous:
            raise ELFError('ambiguous_mapping')
        if rva < 0 or rva+size > len(self.data):
            raise ELFError('unmapped_rva')
        return rva

    def read_rva(self, rva, size, *, executable=True):
        offset = self.rva_to_offset(rva, size, executable=executable)
        return self.data[offset:offset+size]

    def iter_executable_chunks(self, chunk_size=65536, overlap=0):
        if self.image_vaddr:
            raise ELFError('nonzero_image_vaddr')
        for start in range(0, len(self.data), chunk_size-overlap):
            yield start, self.data[start:start+chunk_size]


def data_with(*locations, size=16384, pattern=PATTERN):
    value = bytearray(size)
    for location in locations:
        value[location:location+len(pattern)] = pattern
    return bytes(value)


def definition(data, rva=100):
    return dict(version='4.1.13.26', arch='x86_64', backend='test_v1',
                sha256=hashlib.sha256(data).hexdigest(),
                functions={hex(rva): PATTERN.hex()})


def real_elf(*locations):
    """PT_LOAD mapping intentionally has vaddr != file offset in code segment."""
    data = bytearray(0x2000)
    ident = b'\x7fELF'+bytes([2, 1, 1, 0])+bytes(8)
    data[:64] = struct.pack('<16sHHIQQQIHHHHHH', ident, 3, 62, 1, 0, 64, 0, 0, 64, 56, 2, 0, 0, 0)
    data[64:120] = struct.pack('<IIQQQQQQ', 1, 4, 0, 0, 0, 0x1000, 0x1000, 0x1000)
    data[120:176] = struct.pack('<IIQQQQQQ', 1, 5, 0x1000, 0x2000, 0x2000, 0x1000, 0x1000, 0x1000)
    for offset in locations:
        data[offset:offset+len(PATTERN)] = PATTERN
    return bytes(data)


class ProfileAuditTests(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parents[1]/'.test-tmp'
        parent.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_packaged_profiles_normalize_both_layouts(self):
        text, image = load_profile('text'), load_profile('image')
        self.assertEqual((len(text.functions), len(image.functions)), (10, 21))
        self.assertEqual(text.sha256, image.sha256)
        self.assertEqual(text.arch, 'x86_64')
        self.assertEqual(text.source, 'bundled')
        self.assertEqual(len({item.rva for item in image.functions}), 21)

    def test_profile_requires_unique_valid_sha_arch_and_bounded_patterns(self):
        data = data_with(100)
        base = definition(data)
        changes = [dict(sha256='z'*64), dict(arch='unknown'), dict(schema_version=True),
                   dict(sample_sha256='0'*64), dict(functions={}),
                   dict(functions={'0x1':'aa'*7}), dict(functions={'-1':'aa'*16}),
                   dict(functions=[dict(rva=1, bytes='aa'*16), dict(rva='0x1', bytes='bb'*16)]),
                   dict(functions={str(i):'aa'*16 for i in range(513)})]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ProfileError):
                normalize_profile({**deepcopy(base), **change})
        with self.assertRaises(ProfileError):
            Fingerprint(0, b'1234567')

    def test_custom_profile_is_explicit_bounded_and_rejects_duplicate_keys(self):
        path = self.root/'profile.json'
        path.write_text(json.dumps(definition(data_with(100))), encoding='utf-8')
        self.assertEqual(load_profile_file(path).source, 'custom')
        path.write_bytes(b'{"sha256": 1, "sha256": 2}')
        with self.assertRaisesRegex(ProfileError, 'duplicate_profile_key'):
            load_profile_file(path)
        path.write_bytes(b'x'*(256*1024+1))
        with self.assertRaisesRegex(ProfileError, 'profile_too_large'):
            load_profile_file(path)
        with self.assertRaisesRegex(ProfileError, 'unknown_profile'):
            load_profile('missing')

    def test_full_static_match_requires_hash_arch_and_all_fingerprints(self):
        data = data_with(100)
        profile = normalize_profile(definition(data))
        match = audit_image(SyntheticELF(data), profile)
        self.assertTrue(match['static_match'])
        self.assertIsNone(match['runtime_compatible'])
        self.assertIn('not proof', match['notice'])
        self.assertEqual(match['fingerprints'][0]['status'], 'match')
        changed = bytearray(data);changed[100] ^= 1
        result = audit_image(SyntheticELF(changed), profile)
        self.assertFalse(result['static_match'])
        self.assertFalse(result['checks']['sha256_match'])
        self.assertEqual(result['fingerprints'][0]['status'], 'mismatch')
        result = audit_image(SyntheticELF(data, arch='aarch64'), profile)
        self.assertFalse(result['static_match'])
        self.assertTrue(result['checks']['all_fingerprints_match'])
        self.assertFalse(result['checks']['architecture_match'])

    def test_hash_change_alone_never_becomes_supported(self):
        data = data_with(100)
        profile = normalize_profile(definition(data))
        altered = bytearray(data);altered[-1] = 1
        result = audit_image(SyntheticELF(altered), profile)
        self.assertTrue(result['checks']['all_fingerprints_match'])
        self.assertFalse(result['checks']['sha256_match'])
        self.assertFalse(result['static_match'])

    def test_core_file_never_becomes_an_eligible_executable(self):
        data = data_with(100)
        profile = normalize_profile(definition(data))
        candidate = SyntheticELF(data)
        candidate.elf_type = 'ET_CORE'
        result = audit_image(candidate, profile)
        self.assertFalse(result['static_match'])
        self.assertFalse(result['checks']['elf_type_supported'])
        self.assertEqual(find_fingerprint(candidate, profile, 100)['status'], 'unsupported_elf_type')

    def test_unmapped_and_nonzero_base_are_unknown_not_match(self):
        data = data_with(100)
        profile = normalize_profile(definition(data))
        for candidate, expected in [(SyntheticELF(data[:100]), 'unmapped_rva'),
                                    (SyntheticELF(data, image_vaddr=4096), 'nonzero_image_vaddr')]:
            result = audit_image(candidate, profile)
            self.assertFalse(result['static_match'])
            self.assertEqual(result['fingerprints'][0]['status'], 'unavailable')
            self.assertEqual(result['fingerprints'][0]['error'], expected)

    def test_comparison_marks_reference_match_and_same_rva_mismatch(self):
        original = data_with(100)
        profile = normalize_profile(definition(original))
        candidate = data_with(200)
        result = compare_images(SyntheticELF(original), SyntheticELF(candidate), profile)
        self.assertTrue(result['reference_matches_profile'])
        self.assertFalse(result['candidate_matches_profile'])
        self.assertFalse(result['same_sha256'])
        self.assertFalse(result['fingerprints'][0]['same_bytes_at_rva'])
        json.dumps(result)

    def test_find_reports_relocation_candidate_but_does_not_claim_compatibility(self):
        original = data_with(100)
        candidate = data_with(4090)
        profile = normalize_profile(definition(original))
        result = find_fingerprint(SyntheticELF(candidate), profile, '0x64', chunk_size=4096)
        self.assertEqual(result['matches'], [dict(rva='0xffa', file_offset='0xffa')])
        self.assertTrue(result['unique_candidate'])
        self.assertFalse(result['truncated'])
        self.assertIsNone(result['runtime_compatible'])
        self.assertIn('not proof', result['notice'])

    def test_find_deduplicates_overlapping_chunks_and_reports_truncation(self):
        original = data_with(100)
        profile = normalize_profile(definition(original))
        # The first match occurs in both adjacent chunks; it counts only once.
        candidate = data_with(4082, 8200)
        one = find_fingerprint(SyntheticELF(candidate), profile, 100, limit=1, chunk_size=4096)
        self.assertEqual(len(one['matches']), 1)
        self.assertTrue(one['truncated'])
        self.assertFalse(one['unique_candidate'])
        both = find_fingerprint(SyntheticELF(candidate), profile, 100, limit=2, chunk_size=4096)
        self.assertEqual(len(both['matches']), 2)
        self.assertFalse(both['truncated'])
        self.assertFalse(both['unique_candidate'])

    def test_ambiguous_mapping_cannot_be_reported_as_unique(self):
        original = data_with(100)
        profile = normalize_profile(definition(original))
        candidate = SyntheticELF(data_with(200, 400), ambiguous=[200])
        result = find_fingerprint(candidate, profile, 100, chunk_size=4096)
        self.assertEqual(result['matches'], [dict(rva=hex(400), file_offset=hex(400))])
        self.assertEqual(result['unresolved_mappings'], 1)
        self.assertFalse(result['unique_candidate'])

    def test_scan_limit_never_reports_incomplete_search_as_unique(self):
        original = data_with(100)
        profile = normalize_profile(definition(original))
        candidate = SyntheticELF(data_with(200))
        with patch('wechat_linux.analysis.audit.MAX_SCAN_BYTES', 5000):
            result = find_fingerprint(candidate, profile, 100, chunk_size=4096)
        self.assertEqual(result['status'], 'scan_limit')
        self.assertTrue(result['truncated'])
        self.assertFalse(result['unique_candidate'])
        self.assertEqual(result['scanned_bytes'], 4096)

    def test_extended_reference_anchor_disambiguates_common_prologue(self):
        original = bytearray(data_with(100))
        original[116:132] = bytes(range(16))
        profile = normalize_profile(definition(original))
        candidate = bytearray(data_with(200, 400))
        candidate[216:232] = bytes(range(16))
        short = find_fingerprint(SyntheticELF(candidate), profile, 100, chunk_size=4096)
        self.assertEqual(len(short['matches']), 2)
        self.assertFalse(short['unique_candidate'])
        extended = find_fingerprint(SyntheticELF(candidate), profile, 100, chunk_size=4096,
                                    reference=SyntheticELF(original), pattern_bytes=32)
        self.assertEqual(extended['matches'], [dict(rva=hex(200), file_offset=hex(200))])
        self.assertTrue(extended['unique_candidate'])
        self.assertEqual(extended['anchor_source'], 'verified_reference_bytes')
        self.assertEqual(extended['anchor_length'], 32)
        self.assertEqual(extended['reference_sha256'], profile.sha256)
        self.assertIn('relocations', extended['anchor_notice'])
        self.assertIsNone(extended['runtime_compatible'])

    def test_extended_anchor_requires_a_matching_reference_and_bounded_length(self):
        original = data_with(100)
        profile = normalize_profile(definition(original))
        candidate, reference = SyntheticELF(original), SyntheticELF(original)
        for ref, length, error in [(None, 32, 'reference_anchor_pair_required'),
                                   (reference, None, 'reference_anchor_pair_required'),
                                   (reference, 15, 'invalid_anchor_length'),
                                   (reference, 129, 'invalid_anchor_length'),
                                   (reference, True, 'invalid_anchor_length'),
                                   (SyntheticELF(data_with(200)), 32, 'reference_profile_mismatch')]:
            with self.subTest(length=length, error=error), self.assertRaisesRegex(ProfileError, error):
                find_fingerprint(candidate, profile, 100, reference=ref, pattern_bytes=length)

    def test_extended_anchor_cannot_hide_multiple_hits_or_segment_end(self):
        original = bytearray(data_with(100))
        original[116:132] = bytes(range(16))
        profile = normalize_profile(definition(original))
        candidate = bytearray(data_with(200, 400))
        candidate[216:232] = bytes(range(16));candidate[416:432] = bytes(range(16))
        result = find_fingerprint(SyntheticELF(candidate), profile, 100, limit=1,
                                  reference=SyntheticELF(original), pattern_bytes=32)
        self.assertTrue(result['truncated'])
        self.assertFalse(result['unique_candidate'])
        reference = SyntheticELF(data_with(len(original)-len(PATTERN)))
        profile_at_end = normalize_profile(definition(reference.data, rva=len(original)-len(PATTERN)))
        with self.assertRaisesRegex(ELFError, 'unmapped_rva'):
            find_fingerprint(SyntheticELF(candidate), profile_at_end, len(original)-len(PATTERN),
                             reference=reference, pattern_bytes=32)

    def test_search_rejects_unlisted_rva_bad_limits_and_wrong_architecture(self):
        original = data_with(100)
        profile = normalize_profile(definition(original))
        for args in [(101, 10, 4096), (100, 0, 4096), (100, 101, 4096), (100, 10, 10)]:
            with self.subTest(args=args), self.assertRaises(ProfileError):
                find_fingerprint(SyntheticELF(original), profile, args[0], limit=args[1], chunk_size=args[2])
        result = find_fingerprint(SyntheticELF(original, arch='aarch64'), profile, 100)
        self.assertEqual(result['status'], 'architecture_mismatch')
        self.assertFalse(result['unique_candidate'])
        self.assertEqual(result['matches'], [])

    def test_changed_file_during_audit_never_emits_a_success(self):
        data = data_with(100)
        profile = normalize_profile(definition(data))
        class Changed(SyntheticELF):
            def __init__(self, data):
                super().__init__(data);self.checks = 0
            def check_unchanged(self):
                self.checks += 1
                if self.checks > 1:
                    raise ELFError('file_changed')
        with self.assertRaisesRegex(ELFError, 'file_changed'):
            audit_image(Changed(data), profile)

    def test_real_elf_reader_audits_and_finds_relocated_bytes_with_mapping(self):
        original, updated = real_elf(0x1100), real_elf(0x1300)
        reference_path, candidate_path = self.root/'old.elf', self.root/'new.elf'
        reference_path.write_bytes(original)
        candidate_path.write_bytes(updated)
        profile = normalize_profile(definition(original, rva=0x2100))
        with ELFFile(reference_path) as reference, ELFFile(candidate_path) as candidate:
            check = audit_image(reference, profile)
            self.assertTrue(check['static_match'])
            self.assertEqual(check['fingerprints'][0]['file_offset'], '0x1100')
            comparison = compare_images(reference, candidate, profile)
            self.assertTrue(comparison['reference_matches_profile'])
            self.assertFalse(comparison['candidate_matches_profile'])
            search = find_fingerprint(candidate, profile, 0x2100, chunk_size=4096)
            self.assertEqual(search['matches'], [dict(rva='0x2300', file_offset='0x1300')])
            self.assertTrue(search['unique_candidate'])


if __name__ == '__main__':
    unittest.main()
