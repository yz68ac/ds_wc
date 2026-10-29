"""Static native asset inventory: synthetic source only, no JS execution/client."""
from copy import deepcopy
from importlib.resources import files
import json
import subprocess
import sys
import unittest
from unittest.mock import patch

from wechat_linux.analysis.inventory import InventoryError, inspect_assets, inventory


def sample(*, dictionary=False):
    functions = {'0x100': '1234567890abcdef'} if dictionary else [dict(rva=0x100, bytes='1234567890abcdef')]
    return dict(version='synthetic-1', arch='x86_64', backend='service_v1', sha256='a'*64,
                functions=functions, descriptor_rva='0x200', nested=dict(callback_rva=0x300),
                key_hex='SYNTHETIC_OPAQUE_DO_NOT_ECHO', schema_version=1)


class InventoryTests(unittest.TestCase):
    def test_both_profile_layouts_identify_declared_fingerprints_without_mutation(self):
        source = 'native(0x100, "pointer", []); base.add(0x200); base.add(PROFILE.descriptor_rva);'
        for dictionary in (False, True):
            profile = sample(dictionary=dictionary)
            before = deepcopy(profile)
            result = inspect_assets(profile, source)
            self.assertEqual(profile, before)
            self.assertEqual(result['functions'], [dict(rva=256, hex='0x100', fingerprint_bytes=8,
                                                       fingerprint_hex='1234567890abcdef')])
            expressions = result['module_expressions']
            self.assertEqual([e['rva'] for e in expressions], [0x100, 0x200, 0x200])
            self.assertEqual([e['has_function_fingerprint'] for e in expressions], [True, False, False])
            self.assertEqual(expressions[2]['profile_field'], 'descriptor_rva')

    def test_comments_strings_templates_regex_and_property_names_are_not_literal_call_evidence(self):
        source = r'''
// native(0xdead); PROFILE.missing
/* base.add(0xbeef); PROFILE.missing */
const first = 'native(0x111); PROFILE.missing';
const second = "base.add(0x222)";
const third = `PROFILE.missing ${base.add(0x333)}`;
const pattern = /native\(0x444\).*PROFILE.missing/;
obj.native(0x555); obj.base.add(0x666); $base.add(0x777);
obj.PROFILE.missing;
native ( 0x100, 'pointer', [] );
base . add ( 0x200 );
'''
        result = inspect_assets(sample(), source)
        self.assertEqual([e['rva'] for e in result['module_expressions']], [0x100, 0x200])
        self.assertEqual(result['profile_references'], [])
        self.assertEqual([e['line'] for e in result['module_expressions']], [10, 11])

    def test_computed_calls_and_profile_collections_are_reported_without_guessing(self):
        source = '''base.add(offset); base.add(0x100 + delta); native(pick(), 'void', []);
base.add(PROFILE.nested.callback_rva); const f=PROFILE.functions;
base.add(PROFILE.missing); const x=PROFILE["descriptor_rva"];'''
        result = inspect_assets(sample(), source)
        expressions = result['module_expressions']
        self.assertEqual(result['summary']['unresolved_expressions'], 3)
        self.assertEqual(expressions[3]['rva'], 0x300)
        self.assertFalse(expressions[-1]['field_present'])
        self.assertNotIn('rva', expressions[-1])
        self.assertTrue(result['dynamic_profile_bracket_access'])
        refs = {r['path']: r for r in result['profile_references']}
        self.assertEqual(refs['functions']['value_type'], 'list')
        self.assertFalse(refs['missing']['field_present'])
        self.assertEqual(refs['nested.callback_rva']['value_type'], 'int')

    def test_layout_numbers_are_not_presented_as_module_addresses_or_verified_abi(self):
        source = 'base.add(0x100); block.add(0x100); alloc(0x610);'
        c_source = '/* 0x999 */ struct Input { char data[128]; }; int k = 0x68; char *s="0x888";'
        result = inspect_assets(sample(), source, c_source)
        self.assertEqual(len(result['module_expressions']), 1)
        self.assertEqual({v['value'] for v in result['other_js_hex_literals']}, {0x100, 0x610})
        self.assertEqual({v['value'] for v in result['c_integer_literals']}, {128, 0x68})
        for value in [*result['other_js_hex_literals'], *result['c_integer_literals']]:
            self.assertEqual(value['kind'], 'unclassified_integer_not_assumed_rva')
            self.assertNotIn('has_function_fingerprint', value)

    def test_opaque_profile_values_and_source_lines_do_not_leak_into_json(self):
        profile = sample()
        source = 'const marker="SYNTHETIC_PRIVATE_SOURCE"; globalThis.shouldNotExecute(); base.add(0x100);'
        result = inspect_assets(profile, source)
        encoded = json.dumps(result, allow_nan=False)
        self.assertNotIn('SYNTHETIC_PRIVATE_SOURCE', encoded)
        self.assertNotIn(profile['key_hex'], encoded)
        key = next(c for c in result['profile_constants'] if c['path'] == 'key_hex')
        self.assertEqual(key['kind'], 'opaque_text')
        self.assertTrue(key['value_omitted'])
        nested = next(c for c in result['profile_constants'] if c['path'] == 'nested.callback_rva')
        self.assertEqual(nested['rva'], 0x300)
        self.assertFalse(nested['has_function_fingerprint'])

    def test_input_and_output_work_are_bounded_and_errors_have_fixed_codes(self):
        with patch('wechat_linux.analysis.inventory.MAX_SOURCE_BYTES', 20):
            with self.assertRaisesRegex(InventoryError, '^inventory_source_too_large_or_invalid$'):
                inspect_assets(sample(), 'x'*21)
            with self.assertRaises(InventoryError):
                inspect_assets(sample(), '界'*10)
        with patch('wechat_linux.analysis.inventory.MAX_REFERENCES', 2):
            with self.assertRaisesRegex(InventoryError, '^inventory_reference_limit$'):
                inspect_assets(sample(), 'base.add(0x100);'*3)
        profile = sample()
        profile['extra'] = float('nan')
        with self.assertRaisesRegex(InventoryError, '^inventory_invalid_constant$'):
            inspect_assets(profile, '')
        for backend in ('../profile.json', '/private/path', 1, []):
            with self.assertRaisesRegex(InventoryError, '^inventory_unknown_backend$'):
                inventory(backend)

    def test_packaged_resources_both_backends_and_aliases_have_same_envelope(self):
        result = inventory()
        self.assertEqual(set(result['backends']), {'text', 'image'})
        self.assertFalse(result['executed'])
        self.assertFalse(result['runtime_compatibility_proven'])
        self.assertIn('not a full JS/C parser', ' '.join(result['notes']))
        text, image = result['backends']['text'], result['backends']['image']
        self.assertEqual(text['summary']['function_fingerprints'], 12)
        self.assertEqual(image['summary']['function_fingerprints'], 21)
        self.assertTrue(text['module_expressions'])
        self.assertTrue(image['c_integer_literals'])
        self.assertTrue(any(r['path'] == 'functions' for r in image['profile_references']))
        self.assertEqual(text['profile']['source'], 'bundled')
        for item in (text, image):
            self.assertEqual(item['profile']['version'], '4.1.13.26')
            for asset in item['assets']:
                self.assertEqual(len(asset['sha256']), 64)
                self.assertGreater(asset['bytes'], 0)
        for name, alias in (('text', 'service_v1'), ('image', 'image_coroutine_v1')):
            self.assertEqual(inventory(name), inventory(alias))
            self.assertEqual(set(inventory(name)['backends']), {name})
        json.dumps(result, allow_nan=False)

    def test_resource_loading_uses_package_reader_and_import_does_not_load_runtime(self):
        code = '''
import sys
from wechat_linux.analysis.inventory import inventory
result = inventory('text')
assert result['backends']['text']['profile']['version'] == '4.1.13.26'
assert not any(n.startswith(('wxbridge', 'frida', 'gi')) for n in sys.modules)
assert 'wechat_linux.native.helper' not in sys.modules
assert 'wechat_linux.desktop.ui' not in sys.modules
'''
        result = subprocess.run([sys.executable, '-X', 'utf8', '-B', '-c', code], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        with patch('wechat_linux.analysis.inventory.files', wraps=files) as reader:
            inventory('text')
        self.assertEqual([call.args for call in reader.call_args_list], [('wechat_linux.native',)]*2)


if __name__ == '__main__':
    unittest.main()
