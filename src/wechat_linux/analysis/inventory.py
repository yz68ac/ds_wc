"""Bounded static inventory of packaged native profiles and JS/C source assets.

This is a lexical study aid, not a JavaScript parser or a compatibility test.
Source is never evaluated; opaque profile strings and source lines are omitted.
"""
from __future__ import annotations

from bisect import bisect_right
import hashlib
from importlib.resources import files
import json
import math
import re

from wechat_linux.analysis.audit import normalize_profile

MAX_SOURCE_BYTES = 512 * 1024
MAX_REFERENCES = 4096
MAX_CONSTANTS = 2048
_ASSETS = {
    'text': ('profile.json', 'agent.js', None),
    'image': ('image_profile.json', 'image_agent.js', 'image_adapter.c'),
}
_ALIASES = {'service_v1': 'text', 'image_coroutine_v1': 'image', 'text': 'text', 'image': 'image'}
_CALL = re.compile(r'(?<![\w.$])(?P<call>base\s*\.\s*add|native)\s*\(')
_FIELD = re.compile(r'(?<![\w.$])PROFILE(?P<path>(?:\s*\.\s*[A-Za-z_$][A-Za-z0-9_$]*)+)')
_NUMBER = re.compile(r'0[xX][0-9a-fA-F]+\Z')
_HEX_LITERAL = re.compile(r'(?<![\w.])0[xX][0-9a-fA-F]+(?![\w.])')
_C_LITERAL = re.compile(r'(?<![\w.])(?:0[xX][0-9a-fA-F]+|[0-9]+)(?:[uUlL]{0,3})(?![\w.])')
_IDENTIFIER = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,63}\Z')
NOTES = [
    'RVA means an offset from the loaded module base; it is not a file offset or a live pointer.',
    'A profile function fingerprint is declared expected bytes, not proof that this binary or ABI matches.',
    'Literal calls are syntactic matches only: a base/native identifier can be shadowed; no function role is inferred.',
    'This bounded lexical scan omits comments, quoted strings, templates and common regex literals; it is not a full JS/C parser.',
    'Computed expressions, indirect calls, vtables, object layouts and decimal JS field/size constants are not fully inventoried.',
    'No source is executed, no process is attached and no profile is created, modified or approved.',
]


class InventoryError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _source(value):
    if not isinstance(value, str) or len(value) > MAX_SOURCE_BYTES:
        raise InventoryError('inventory_source_too_large_or_invalid')
    try:
        size = len(value.encode('utf-8'))
    except UnicodeError:
        raise InventoryError('inventory_source_encoding') from None
    if size > MAX_SOURCE_BYTES:
        raise InventoryError('inventory_source_too_large_or_invalid')
    return value


def _mask(source, *, javascript=True):
    """Preserve positions/newlines while omitting common non-code lexemes."""
    out = list(source)
    index, previous = 0, ''
    while index < len(source):
        start, char = index, source[index]
        if char.isspace():
            index += 1
            continue
        if source.startswith('//', index):
            end = source.find('\n', index)
            index = len(source) if end < 0 else end
        elif source.startswith('/*', index):
            end = source.find('*/', index+2)
            index = len(source) if end < 0 else end+2
        elif char in ('"', "'", '`'):
            index += 1
            while index < len(source):
                if source[index] == '\\':
                    index += 2
                elif source[index] == char:
                    index += 1
                    break
                else:
                    index += 1
            previous = 'literal'
        elif (javascript and char == '/' and previous in
              ('', '=', '(', '[', '{', ',', ':', ';', '!', '?', '&', '|', 'return', 'throw', 'case', 'yield')):
            index += 1
            in_class, closed = False, False
            while index < len(source) and source[index] not in '\r\n':
                if source[index] == '\\':
                    index += 2
                    continue
                if source[index] == '[':
                    in_class = True
                elif source[index] == ']':
                    in_class = False
                elif source[index] == '/' and not in_class:
                    index += 1
                    while index < len(source) and source[index].isalpha():
                        index += 1
                    closed = True
                    break
                index += 1
            if not closed:
                index = start+1
                previous = char
                continue
            previous = 'literal'
        elif char.isalpha() or char in '_$':
            index += 1
            while index < len(source) and (source[index].isalnum() or source[index] in '_$'):
                index += 1
            previous = source[start:index]
            continue
        else:
            previous = char
            index += 1
            continue
        for pos in range(start, min(index, len(source))):
            if source[pos] not in '\r\n':
                out[pos] = ' '
    return ''.join(out)


def _resolve(profile, path):
    value = profile
    for field in path.split('.'):
        if not isinstance(value, dict) or field not in value:
            return None, False
        value = value[field]
    return value, True


def _address(value):
    if type(value) is int and 0 <= value <= (1 << 64)-1:
        return value
    if isinstance(value, str) and _NUMBER.fullmatch(value) and len(value) <= 18:
        return int(value, 16)
    return None


def _constants(profile, fingerprinted):
    result = []

    def walk(value, path='', depth=0):
        if depth > 8 or len(result) >= MAX_CONSTANTS:
            raise InventoryError('inventory_constant_limit')
        if isinstance(value, dict):
            for key, item in value.items():
                if not isinstance(key, str) or not _IDENTIFIER.fullmatch(key):
                    raise InventoryError('inventory_invalid_constant_name')
                if not path and key == 'functions':
                    continue
                walk(item, path+'.'+key if path else key, depth+1)
            return
        address = _address(value) if path.rsplit('.', 1)[-1].endswith('_rva') else None
        if address is not None:
            item = dict(path=path, kind='module_rva', rva=address, hex=hex(address),
                        has_function_fingerprint=address in fingerprinted)
        elif type(value) is float and not math.isfinite(value):
            raise InventoryError('inventory_invalid_constant')
        elif type(value) in (bool, int, float) or value is None:
            item = dict(path=path, kind='scalar', value=value)
        elif isinstance(value, str):
            item = dict(path=path, kind='opaque_text', characters=len(value), value_omitted=True)
        elif isinstance(value, list):
            item = dict(path=path, kind='collection', items=len(value), value_omitted=True)
        else:
            raise InventoryError('inventory_invalid_constant')
        result.append(item)

    walk(profile)
    return result


def _argument(masked, start):
    depth = 0
    for end in range(start, min(len(masked), start+256)):
        char = masked[end]
        if char == '(':
            depth += 1
        elif char == ')':
            if depth == 0:
                return masked[start:end].strip(), end, True
            depth -= 1
        elif char == ',' and depth == 0:
            return masked[start:end].strip(), end, True
    return '', start, False


def _literals(masked, pattern, newline_positions, *, omit_spans=()):
    grouped = {}
    omit_spans = sorted(omit_spans)
    span_index = 0
    for index, match in enumerate(pattern.finditer(masked)):
        if index >= MAX_REFERENCES:
            raise InventoryError('inventory_reference_limit')
        while span_index < len(omit_spans) and omit_spans[span_index][1] <= match.start():
            span_index += 1
        if span_index < len(omit_spans) and omit_spans[span_index][0] <= match.start() < omit_spans[span_index][1]:
            continue
        if len(grouped) >= MAX_REFERENCES:
            raise InventoryError('inventory_reference_limit')
        literal = match.group()
        number = literal.rstrip('uUlL')
        if len(number) > 18:
            continue
        value = int(number, 16 if number.lower().startswith('0x') else 10)
        entry = grouped.setdefault(value, dict(value=value, hex=hex(value), lines=[], occurrences=0,
                                              kind='unclassified_integer_not_assumed_rva'))
        entry['occurrences'] += 1
        line = bisect_right(newline_positions, match.start())+1
        if len(entry['lines']) < 16 and line not in entry['lines']:
            entry['lines'].append(line)
    return list(grouped.values())


def inspect_assets(profile, agent_source, c_source=None, *, name='custom', source='custom'):
    """Inspect explicitly supplied mappings/source strings; return JSON-safe data."""
    normalized = normalize_profile(profile, name=name, source=source)
    agent_source = _source(agent_source)
    if c_source is not None:
        c_source = _source(c_source)
    functions = {f.rva: f for f in normalized.functions}
    masked = _mask(agent_source)
    newlines = [m.start() for m in re.finditer('\n', masked)]
    calls, literal_spans, references = [], [], []
    for match in _CALL.finditer(masked):
        if len(calls) >= MAX_REFERENCES:
            raise InventoryError('inventory_reference_limit')
        argument, end, complete = _argument(masked, match.end())
        item = dict(call='base.add' if match['call'].startswith('base') else 'native',
                    line=bisect_right(newlines, match.start())+1, kind='dynamic_or_unsupported_expression')
        address = _address(argument) if complete and _NUMBER.fullmatch(argument) else None
        field = _FIELD.fullmatch(argument) if complete else None
        if address is not None:
            item.update(kind='literal_module_rva', rva=address, hex=hex(address),
                        has_function_fingerprint=address in functions)
            literal_spans.append((match.end(), end))
        elif field:
            path = re.sub(r'\s+', '', field['path'])[1:]
            value, found = _resolve(profile, path)
            address = _address(value) if found else None
            item.update(kind='profile_field_expression', profile_field=path, field_present=found)
            if address is not None:
                item.update(rva=address, hex=hex(address), has_function_fingerprint=address in functions)
        calls.append(item)
    for match in _FIELD.finditer(masked):
        if len(references) >= MAX_REFERENCES:
            raise InventoryError('inventory_reference_limit')
        path = re.sub(r'\s+', '', match['path'])[1:]
        value, found = _resolve(profile, path)
        references.append(dict(path=path, line=bisect_right(newlines, match.start())+1,
                               field_present=found, value_type=type(value).__name__ if found else 'missing'))
    result = dict(name=name, profile=normalized.summary(),
                  functions=[dict(rva=f.rva, hex=hex(f.rva), fingerprint_bytes=len(f.pattern),
                                  fingerprint_hex=f.pattern.hex()) for f in normalized.functions],
                  profile_constants=_constants(profile, functions), module_expressions=calls,
                  profile_references=references,
                  other_js_hex_literals=_literals(masked, _HEX_LITERAL, newlines, omit_spans=literal_spans),
                  dynamic_profile_bracket_access=bool(re.search(r'(?<![\w.$])PROFILE\s*\[', masked)))
    if c_source is not None:
        c_masked = _mask(c_source, javascript=False)
        positions = [m.start() for m in re.finditer('\n', c_masked)]
        result['c_integer_literals'] = _literals(c_masked, _C_LITERAL, positions)
    result['summary'] = dict(function_fingerprints=len(functions), module_expressions=len(calls),
                            literal_rvas=sum(c['kind'] == 'literal_module_rva' for c in calls),
                            expressions_with_function_fingerprint=sum(c.get('has_function_fingerprint', False) for c in calls),
                            unresolved_expressions=sum(c['kind'] == 'dynamic_or_unsupported_expression' for c in calls))
    return result


def _asset(name):
    try:
        with files('wechat_linux.native').joinpath(name).open('rb') as stream:
            raw = stream.read(MAX_SOURCE_BYTES+1)
    except OSError:
        raise InventoryError('inventory_asset_unavailable') from None
    if len(raw) > MAX_SOURCE_BYTES:
        raise InventoryError('inventory_source_too_large_or_invalid')
    try:
        return raw, raw.decode('utf-8')
    except UnicodeError:
        raise InventoryError('inventory_source_encoding') from None


def inventory(backend=None):
    """Inventory packaged text/image assets only; a backend narrows the same envelope."""
    if backend is not None and (not isinstance(backend, str) or backend not in _ALIASES):
        raise InventoryError('inventory_unknown_backend')
    names = [_ALIASES[backend]] if backend is not None else list(_ASSETS)
    result = dict(schema_version=1, kind='static_asset_inventory', executed=False,
                  runtime_compatibility_proven=False, notes=list(NOTES), backends={})
    for name in names:
        profile_name, agent_name, c_name = _ASSETS[name]
        raw_profile, profile_text = _asset(profile_name)
        raw_agent, agent_text = _asset(agent_name)
        raw_c, c_text = _asset(c_name) if c_name else (None, None)
        try:
            profile = json.loads(profile_text)
        except (ValueError, RecursionError):
            raise InventoryError('inventory_profile_json_invalid') from None
        item = inspect_assets(profile, agent_text, c_text, name=name, source='bundled')
        item['assets'] = [dict(name=asset_name, bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
                          for asset_name, raw in ((profile_name, raw_profile), (agent_name, raw_agent), (c_name, raw_c))
                          if raw is not None]
        result['backends'][name] = item
    return result
