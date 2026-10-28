"""Read-only, bounded fingerprint analysis for explicitly supplied ELF files.

Results are static evidence, never authorization to attach or call a function.
The caller owns the open ELFFile and controls its lifetime.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from importlib.resources import files
import json
import os
from pathlib import Path
import re
import stat

from wechat_linux.analysis.elf import ELFError

MAX_PROFILE_BYTES = 256 * 1024
MAX_FUNCTIONS = 512
MIN_PATTERN_BYTES = 8
MAX_PATTERN_BYTES = 64
MAX_ANCHOR_BYTES = 128
MAX_SCAN_BYTES = 512 * 1024 * 1024
MAX_U64 = (1 << 64) - 1
NOTICE = ('Static fingerprint analysis only; a match or search candidate is not proof of '
          'runtime compatibility, correct ABI, object ownership, or safe invocation.')
_BUILTINS = {'text': 'profile.json', 'image': 'image_profile.json'}
_SHA = re.compile(r'[0-9a-fA-F]{64}\Z')
_HEX = re.compile(r'(?:[0-9a-fA-F]{2})+\Z')
_LABEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.+-]{0,95}\Z')


class ProfileError(ValueError):
    """A fixed diagnostic code; never embeds a path, private data, or input body."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _rva(value):
    if type(value) is int:
        result = value
    elif isinstance(value, str) and re.fullmatch(r'(?:0[xX][0-9a-fA-F]+|[0-9]+)', value):
        try:
            result = int(value, 16 if value.lower().startswith('0x') else 10)
        except ValueError:
            raise ProfileError('invalid_function_rva') from None
    else:
        raise ProfileError('invalid_function_rva')
    if not 0 <= result <= MAX_U64:
        raise ProfileError('invalid_function_rva')
    return result


@dataclass(frozen=True)
class Fingerprint:
    rva: int
    pattern: bytes

    def __post_init__(self):
        if (type(self.rva) is not int or not 0 <= self.rva <= MAX_U64 or type(self.pattern) is not bytes
                or not MIN_PATTERN_BYTES <= len(self.pattern) <= MAX_PATTERN_BYTES
                or self.rva + len(self.pattern) - 1 > MAX_U64):
            raise ProfileError('invalid_fingerprint')


@dataclass(frozen=True)
class Profile:
    name: str
    version: str
    arch: str
    backend: str
    sha256: str
    functions: tuple[Fingerprint, ...]
    source: str = 'custom'

    def __post_init__(self):
        if (not isinstance(self.name, str) or not _LABEL.fullmatch(self.name)
                or not isinstance(self.version, str) or not _LABEL.fullmatch(self.version)
                or self.arch not in ('x86_64', 'aarch64')
                or not isinstance(self.backend, str) or not _LABEL.fullmatch(self.backend)
                or not isinstance(self.sha256, str) or not _SHA.fullmatch(self.sha256)
                or self.source not in ('bundled', 'custom')
                or not isinstance(self.functions, tuple) or not 1 <= len(self.functions) <= MAX_FUNCTIONS
                or any(not isinstance(item, Fingerprint) for item in self.functions)
                or len({item.rva for item in self.functions}) != len(self.functions)):
            raise ProfileError('invalid_profile')
        object.__setattr__(self, 'sha256', self.sha256.lower())

    def summary(self):
        return dict(name=self.name, version=self.version, arch=self.arch, backend=self.backend,
                    sha256=self.sha256, source=self.source, fingerprints=len(self.functions))


def normalize_profile(mapping, *, name='custom', source='custom'):
    """Validate and normalize either existing profile layout, without mutation."""
    if not isinstance(mapping, dict):
        raise ProfileError('invalid_profile')
    schema = mapping.get('schema_version', 1)
    if type(schema) is not int or schema != 1:
        raise ProfileError('unsupported_profile_schema')
    sha = mapping.get('sha256')
    sample = mapping.get('sample_sha256')
    if sample is not None and (not isinstance(sample, str) or not _SHA.fullmatch(sample)
                               or not isinstance(sha, str) or sample.lower() != sha.lower()):
        raise ProfileError('inconsistent_sample_sha256')
    raw = mapping.get('functions')
    if isinstance(raw, dict):
        if not 1 <= len(raw) <= MAX_FUNCTIONS:
            raise ProfileError('invalid_fingerprints')
        entries = list(raw.items())
    elif isinstance(raw, list):
        if not 1 <= len(raw) <= MAX_FUNCTIONS:
            raise ProfileError('invalid_fingerprints')
        if any(not isinstance(item, dict) or 'rva' not in item or 'bytes' not in item for item in raw):
            raise ProfileError('invalid_fingerprints')
        entries = [(item['rva'], item['bytes']) for item in raw]
    else:
        raise ProfileError('invalid_fingerprints')
    normalized = []
    for raw_rva, hex_pattern in entries:
        rva = _rva(raw_rva)
        if (not isinstance(hex_pattern, str) or not _HEX.fullmatch(hex_pattern)
                or not MIN_PATTERN_BYTES * 2 <= len(hex_pattern) <= MAX_PATTERN_BYTES * 2):
            raise ProfileError('invalid_fingerprint')
        normalized.append(Fingerprint(rva, bytes.fromhex(hex_pattern)))
    return Profile(name, mapping.get('version'), mapping.get('arch'), mapping.get('backend'),
                   sha, tuple(sorted(normalized, key=lambda item: item.rva)), source)


def _json_profile(raw, *, name, source):
    if len(raw) > MAX_PROFILE_BYTES:
        raise ProfileError('profile_too_large')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ProfileError('duplicate_profile_key')
            result[key] = value
        return result
    def invalid_constant(_):
        raise ProfileError('invalid_profile_json')
    try:
        value = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=pairs, parse_constant=invalid_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, ProfileError):
            raise
        raise ProfileError('invalid_profile_json') from None
    return normalize_profile(value, name=name, source=source)


def load_profile(name):
    """Load a packaged version profile by its explicit text/image identifier."""
    if not isinstance(name, str) or name not in _BUILTINS:
        raise ProfileError('unknown_profile')
    try:
        with files('wechat_linux.native').joinpath(_BUILTINS[name]).open('rb') as stream:
            raw = stream.read(MAX_PROFILE_BYTES + 1)
    except OSError:
        raise ProfileError('profile_unavailable') from None
    return _json_profile(raw, name=name, source='bundled')


def load_profile_file(path):
    """Read an explicit external profile once; it is not promoted to trusted data."""
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NONBLOCK', 0)
    fd = None
    try:
        fd = os.open(Path(path), flags)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ProfileError('profile_not_regular_file')
        if before.st_size > MAX_PROFILE_BYTES:
            raise ProfileError('profile_too_large')
        with os.fdopen(fd, 'rb') as stream:
            fd = None
            raw = stream.read(MAX_PROFILE_BYTES + 1)
            after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns, before.st_mode, before.st_ino, before.st_dev) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_mode, after.st_ino, after.st_dev):
            raise ProfileError('profile_changed_during_read')
    except ProfileError:
        raise
    except (OSError, ValueError, TypeError):
        raise ProfileError('profile_unavailable') from None
    finally:
        if fd is not None:
            os.close(fd)
    return _json_profile(raw, name='custom', source='custom')


def _require_profile(profile):
    if not isinstance(profile, Profile):
        raise ProfileError('invalid_profile')
    return profile


def _fingerprint(elf, item):
    try:
        offset = elf.rva_to_offset(item.rva, len(item.pattern), executable=True)
        observed = elf.read_rva(item.rva, len(item.pattern), executable=True)
    except ELFError as exc:
        return dict(rva=hex(item.rva), expected=item.pattern.hex(), status='unavailable', error=exc.code)
    return dict(rva=hex(item.rva), file_offset=hex(offset), expected=item.pattern.hex(),
                actual=observed.hex(), status='match' if observed == item.pattern else 'mismatch')


def audit_image(elf, profile):
    """Compare the full ELF hash, architecture and every fingerprint at its RVA."""
    _require_profile(profile)
    elf.check_unchanged()
    sha = elf.sha256()
    entries = [_fingerprint(elf, item) for item in profile.functions]
    elf.check_unchanged()
    hash_match = sha == profile.sha256
    architecture_match = elf.architecture == profile.arch
    all_fingerprints_match = all(item['status'] == 'match' for item in entries)
    type_supported = elf.elf_type in ('ET_DYN', 'ET_EXEC')
    static_match = (hash_match and architecture_match and type_supported
                    and elf.image_vaddr == 0 and all_fingerprints_match)
    return dict(schema_version=1, operation='audit', profile=profile.summary(),
        image=dict(sha256=sha, architecture=elf.architecture, elf_type=elf.elf_type,
                   size=elf.size, image_vaddr=hex(elf.image_vaddr)),
        checks=dict(sha256_match=hash_match, architecture_match=architecture_match, elf_type_supported=type_supported,
                    zero_image_vaddr=elf.image_vaddr == 0, all_fingerprints_match=all_fingerprints_match),
        static_match=static_match, runtime_compatible=None, fingerprints=entries, notice=NOTICE)


def compare_images(reference, candidate, profile):
    """Compare the two files at the known profile RVAs, never auto-relocate them."""
    left = audit_image(reference, profile)
    right = audit_image(candidate, profile)
    reference.check_unchanged()
    candidate.check_unchanged()
    pairs = []
    for first, second in zip(left['fingerprints'], right['fingerprints']):
        same = (first.get('actual') == second.get('actual')
                if first['status'] != 'unavailable' and second['status'] != 'unavailable' else None)
        pairs.append(dict(rva=first['rva'], reference=first, candidate=second, same_bytes_at_rva=same))
    return dict(schema_version=1, operation='compare', profile=profile.summary(),
        reference=left['image'], candidate=right['image'], reference_matches_profile=left['static_match'],
        candidate_matches_profile=right['static_match'], same_sha256=left['image']['sha256'] == right['image']['sha256'],
        runtime_compatible=None, fingerprints=pairs, notice=NOTICE)


def find_fingerprint(elf, profile, rva, *, limit=10, chunk_size=65536, reference=None, pattern_bytes=None):
    """Find one existing fingerprint in executable file-backed PT_LOAD segments.

    Search stops after one extra match beyond the caller's limit so a truncated
    result cannot be confused with a unique candidate. Overlapping chunks and
    ambiguous segment mappings do not produce false uniqueness. Optionally pass
    both reference and pattern_bytes to extend the anchor to 16–128 bytes from a
    reference matching every profile check; never update or synthesize a profile.
    """
    _require_profile(profile)
    address = _rva(rva)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ProfileError('invalid_match_limit')
    if type(chunk_size) is not int or not 4096 <= chunk_size <= 1024 * 1024:
        raise ProfileError('invalid_chunk_size')
    item = next((value for value in profile.functions if value.rva == address), None)
    if item is None:
        raise ProfileError('unknown_profile_rva')
    if (reference is None) != (pattern_bytes is None):
        raise ProfileError('reference_anchor_pair_required')
    pattern = item.pattern
    anchor_source, reference_sha256 = 'profile_fingerprint', None
    if reference is not None:
        if type(pattern_bytes) is not int or not max(16, len(item.pattern)) <= pattern_bytes <= MAX_ANCHOR_BYTES:
            raise ProfileError('invalid_anchor_length')
        verified = audit_image(reference, profile)
        if not verified['static_match']:
            raise ProfileError('reference_profile_mismatch')
        pattern = reference.read_rva(address, pattern_bytes, executable=True)
        if not pattern.startswith(item.pattern):
            raise ProfileError('reference_fingerprint_changed')
        reference.check_unchanged()
        anchor_source = 'verified_reference_bytes'
        reference_sha256 = verified['image']['sha256']
    elf.check_unchanged()
    sha = elf.sha256()
    if elf.elf_type not in ('ET_DYN', 'ET_EXEC') or elf.architecture != profile.arch:
        elf.check_unchanged()
        return dict(schema_version=1, operation='find', profile=profile.summary(),
            image_sha256=sha, image_architecture=elf.architecture, source_rva=hex(address),
            status=('unsupported_elf_type' if elf.elf_type not in ('ET_DYN', 'ET_EXEC') else 'architecture_mismatch'),
            matches=[], truncated=False, unique_candidate=False, scanned_bytes=0,
            unresolved_mappings=0, anchor_source=anchor_source, anchor_length=len(pattern),
            anchor_sha256=hashlib.sha256(pattern).hexdigest(),
            reference_sha256=reference_sha256, runtime_compatible=None, notice=NOTICE,
            anchor_notice='Relative instructions and relocations can change across builds; an exact byte search can miss a real candidate.')
    candidates, seen, unresolved = [], set(), 0
    truncated, scanned_bytes, status = False, 0, 'candidate_search'
    for chunk_rva, data in elf.iter_executable_chunks(chunk_size=chunk_size, overlap=len(pattern)-1):
        if scanned_bytes + len(data) > MAX_SCAN_BYTES:
            truncated, status = True, 'scan_limit'
            break
        scanned_bytes += len(data)
        position = 0
        while True:
            position = data.find(pattern, position)
            if position < 0:
                break
            location = chunk_rva + position
            position += 1
            if location in seen:
                continue
            seen.add(location)
            try:
                file_offset = elf.rva_to_offset(location, len(pattern), executable=True)
            except ELFError:
                unresolved += 1
                # Keep work bounded even if repeated hits are invalid mappings.
                if unresolved > limit:
                    truncated = True
                    break
                continue
            if len(candidates) >= limit:
                truncated = True
                break
            candidates.append(dict(rva=hex(location), file_offset=hex(file_offset)))
        if truncated:
            break
    elf.check_unchanged()
    if reference is not None:
        reference.check_unchanged()
    return dict(schema_version=1, operation='find', profile=profile.summary(), image_sha256=sha,
        image_architecture=elf.architecture, source_rva=hex(address), status=status, scanned_bytes=scanned_bytes,
        matches=candidates, truncated=truncated, unique_candidate=len(candidates) == 1 and not truncated and not unresolved,
        unresolved_mappings=unresolved, anchor_source=anchor_source, anchor_length=len(pattern),
        anchor_sha256=hashlib.sha256(pattern).hexdigest(),
        reference_sha256=reference_sha256, runtime_compatible=None, notice=NOTICE,
        anchor_notice='Relative instructions and relocations can change across builds; an exact byte search can miss a real candidate.')
