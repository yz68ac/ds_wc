"""Bounded, read-only ELF64 inspection. Never loads or executes the input binary.

RVA operations intentionally support only images whose lowest PT_LOAD vaddr is
zero. This matches the fixed research sample; other layouts require an explicit
profile/base convention instead of a guessed mapping.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
import struct


MAX_HEADERS = 4096
MAX_READ = 1024 * 1024
ADDRESS_SPACE = 1 << 64


class ELFError(ValueError):
    def __init__(self, code, message=None):
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class LoadSegment:
    index: int
    offset: int
    vaddr: int
    filesz: int
    memsz: int
    flags: int
    align: int

    @property
    def readable(self):
        return bool(self.flags & 4)

    @property
    def writable(self):
        return bool(self.flags & 2)

    @property
    def executable(self):
        return bool(self.flags & 1)


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class ELFFile:
    """One read-only descriptor, explicit lifetime, safe to inspect untrusted files.

    Calls are synchronous and belong to one thread. SHA-256 is cached only for
    this descriptor and checked against current file/path metadata on reuse.
    """
    def __init__(self, path, *, max_program_headers=MAX_HEADERS):
        if type(max_program_headers) is not int or not 1 <= max_program_headers <= MAX_HEADERS:
            raise ELFError('invalid_header_limit', 'Program-header limit must be between 1 and 4096')
        self.path = Path(path).absolute()
        self.max_program_headers = max_program_headers
        self._stream = None
        self._digest = None

    def __enter__(self):
        if self._stream is not None:
            raise ELFError('elf_already_open', 'This ELF descriptor is already open')
        descriptor = None
        try:
            if self.path.is_symlink():
                raise ELFError('unsafe_elf_path', 'ELF input must be a regular file, not a symbolic link')
            descriptor = os.open(self.path, os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise ELFError('unsafe_elf_path', 'ELF input must be a regular file')
            self._initial = _identity(info)
            self._initial_path = _identity(os.stat(self.path, follow_symlinks=False))
            # Windows may expose different creation/change timestamps through
            # path stat and descriptor fstat. Compare their stable identity now,
            # then retain each interface's own ctime snapshot for later checks.
            if self._initial[:5] != self._initial_path[:5]:
                raise ELFError('elf_changed', 'ELF path no longer identifies the opened input file')
            self.size = info.st_size
            if not 0 <= self.size < ADDRESS_SPACE:
                raise ELFError('invalid_file_size', 'File size exceeds the ELF64 address range')
            self._stream = os.fdopen(descriptor, 'rb', buffering=0)
            descriptor = None
            self._digest = None
            self.check_unchanged()
            self._parse()
            self.check_unchanged()
            return self
        except OSError:
            self.close()
            raise ELFError('elf_io_error', 'ELF input could not be opened or read') from None
        except BaseException:
            self.close()
            raise
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def __exit__(self, kind, value, traceback):
        try:
            if kind is None:
                self.check_unchanged()
        finally:
            self.close()

    def close(self):
        if self._stream is not None:
            stream, self._stream = self._stream, None
            stream.close()

    def _opened(self):
        if self._stream is None:
            raise ELFError('elf_not_open', 'Open ELFFile with a context manager before reading')

    def check_unchanged(self):
        self._opened()
        try:
            current = _identity(os.fstat(self._stream.fileno()))
            named = _identity(os.stat(self.path, follow_symlinks=False))
        except OSError:
            raise ELFError('elf_changed', 'ELF input changed or disappeared during inspection') from None
        if current != self._initial or named != self._initial_path:
            raise ELFError('elf_changed', 'ELF input changed or was replaced during inspection')

    def _read_at(self, offset, size):
        self._opened()
        if type(offset) is not int or type(size) is not int or offset < 0 or not 0 <= size <= MAX_READ or offset+size > self.size:
            raise ELFError('invalid_file_range', 'Requested bytes are outside the bounded file range')
        try:
            self._stream.seek(offset)
            data = self._stream.read(size)
        except OSError:
            raise ELFError('elf_io_error', 'ELF bytes could not be read') from None
        if len(data) != size:
            raise ELFError('truncated_elf', 'ELF input ended before the declared byte range')
        return data

    def _parse(self):
        if self.size < 64:
            raise ELFError('truncated_elf_header', 'ELF64 requires a complete 64-byte header')
        raw = self._read_at(0, 64)
        if raw[:4] != b'\x7fELF':
            raise ELFError('not_elf', 'Input does not have an ELF signature')
        if raw[4] != 2:
            raise ELFError('unsupported_elf_class', 'Only ELF64 inputs are supported')
        if raw[5] != 1:
            raise ELFError('unsupported_endianness', 'Only little-endian ELF inputs are supported')
        if raw[6] != 1:
            raise ELFError('unsupported_elf_version', 'ELF identification version must be 1')
        fields = struct.unpack('<16sHHIQQQIHHHHHH', raw)
        _, kind, self.machine, version, _, phoff, _, _, ehsize, phsize, phnum, _, _, _ = fields
        self.architecture = {62:'x86_64', 183:'aarch64'}.get(self.machine, 'unknown')
        self.elf_type = {0:'ET_NONE', 1:'ET_REL', 2:'ET_EXEC', 3:'ET_DYN', 4:'ET_CORE'}.get(kind, 'ET_UNKNOWN')
        self.elf_type_id = kind
        if version != 1 or ehsize != 64:
            raise ELFError('invalid_elf_header', 'ELF version or declared header size is unsupported')
        if phnum == 0xffff:
            raise ELFError('extended_program_headers_unsupported', 'Extended ELF program-header numbering is not supported')
        if phnum > self.max_program_headers:
            raise ELFError('too_many_program_headers', 'Program-header table exceeds the configured bounded limit')
        if not phnum:
            raise ELFError('missing_load_segments', 'ELF input has no loadable program headers')
        if phsize != 56 or phoff < 64 or phoff+phnum*phsize > self.size:
            raise ELFError('invalid_program_headers', 'Program-header size or table file range is invalid')
        segments = []
        for index in range(phnum):
            tag, flags, offset, vaddr, _, filesz, memsz, align = struct.unpack('<IIQQQQQQ', self._read_at(phoff+index*phsize, 56))
            if tag != 1:
                continue
            if filesz > memsz or offset+filesz > self.size or vaddr+memsz > ADDRESS_SPACE:
                raise ELFError('invalid_load_range', 'PT_LOAD file or memory range is invalid')
            if align not in (0, 1) and (align & (align-1) or vaddr % align != offset % align):
                raise ELFError('invalid_load_alignment', 'PT_LOAD alignment must be a power of two with matching file/virtual offsets')
            segments.append(LoadSegment(index, offset, vaddr, filesz, memsz, flags, align))
        if not segments:
            raise ELFError('missing_load_segments', 'ELF input has no PT_LOAD segments')
        self.segments = tuple(sorted(segments, key=lambda s:(s.vaddr, s.offset, s.index)))
        mapped = [s for s in self.segments if s.memsz]
        if not mapped:
            raise ELFError('empty_load_segments', 'ELF input has no nonempty PT_LOAD memory ranges')
        self.image_vaddr = min(s.vaddr for s in mapped)

    def sha256(self):
        self.check_unchanged()
        if self._digest is None:
            digest = hashlib.sha256()
            position = 0
            while position < self.size:
                count = min(MAX_READ, self.size-position)
                digest.update(self._read_at(position, count))
                position += count
            self.check_unchanged()
            self._digest = digest.hexdigest()
        self.check_unchanged()
        return self._digest

    def _zero_based(self):
        self._opened()
        if self.image_vaddr != 0:
            raise ELFError('nonzero_image_vaddr', 'Profile RVA inspection requires a zero-based PT_LOAD image; no load base was guessed')

    def rva_to_offset(self, rva, size=1, *, executable=True):
        self._zero_based()
        self.check_unchanged()
        if (type(rva) is not int or type(size) is not int or rva < 0 or size < 1
                or rva+size > ADDRESS_SPACE or type(executable) is not bool):
            raise ELFError('invalid_rva_range', 'RVA and size must define a positive ELF64 byte range')
        end = rva+size
        matching = [s for s in self.segments if s.vaddr <= rva and end <= s.vaddr+s.memsz]
        touching = [s for s in self.segments if s.memsz > 0 and rva < s.vaddr+s.memsz and end > s.vaddr]
        if not matching:
            raise ELFError('rva_crosses_segment' if touching else 'rva_unmapped', 'RVA range is not contained in one PT_LOAD segment')
        if len(matching) != 1 or len(touching) != 1:
            raise ELFError('ambiguous_load_range', 'Overlapping PT_LOAD mappings make this RVA ambiguous')
        segment = matching[0]
        if end > segment.vaddr+segment.filesz:
            raise ELFError('rva_not_file_backed', 'RVA range includes BSS or bytes absent from the file')
        if executable and not segment.executable:
            raise ELFError('rva_not_executable', 'Code fingerprints require an executable PT_LOAD segment')
        return segment.offset+rva-segment.vaddr

    def read_rva(self, rva, size, *, executable=True):
        self.check_unchanged()
        if type(size) is not int or not 1 <= size <= MAX_READ:
            raise ELFError('rva_read_limit', 'A single RVA read must be between 1 byte and 1 MiB')
        data = self._read_at(self.rva_to_offset(rva, size, executable=executable), size)
        self.check_unchanged()
        return data

    def iter_executable_chunks(self, chunk_size=65536, overlap=0):
        self._zero_based()
        if (type(chunk_size) is not int or not 1 <= chunk_size <= MAX_READ
                or type(overlap) is not int or not 0 <= overlap < chunk_size):
            raise ELFError('invalid_chunk_options', 'Chunk size must be at most 1 MiB and overlap smaller than the chunk')
        self.check_unchanged()
        for segment in self.segments:
            if not segment.executable:
                continue
            position = 0
            while position < segment.filesz:
                size = min(chunk_size, segment.filesz-position)
                rva = segment.vaddr+position
                yield rva, self.read_rva(rva, size)
                if position+size == segment.filesz:
                    break
                position += size-overlap
        self.check_unchanged()
