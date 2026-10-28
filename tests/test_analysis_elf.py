"""Synthetic ELF fixtures: no client execution, process attachment or live files."""
import hashlib
import os
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from wechat_linux.analysis.elf import ELFError, ELFFile, MAX_READ


def make_elf(segments=None, *, machine=62, elf_type=3, file_size=0x600):
    """Build a small ELF64 fixture; segment fields mirror the public descriptor."""
    if segments is None:
        segments=[dict(offset=0,vaddr=0,filesz=file_size,memsz=file_size+0x100,flags=5,align=0x1000)]
    image=bytearray((i*37) % 256 for i in range(file_size))
    ident=b'\x7fELF'+bytes((2,1,1))+bytes(9)
    image[:64]=struct.pack('<16sHHIQQQIHHHHHH',ident,elf_type,machine,1,0,64,0,0,64,56,len(segments),0,0,0)
    for index, segment in enumerate(segments):
        struct.pack_into('<IIQQQQQQ',image,64+56*index,segment.get('type',1),segment.get('flags',5),
                         segment.get('offset',0),segment.get('vaddr',0),0,segment.get('filesz',0),
                         segment.get('memsz',segment.get('filesz',0)),segment.get('align',1))
    return bytes(image)


class ELFInspectionTests(unittest.TestCase):
    def setUp(self):
        parent=Path(__file__).resolve().parents[1]/'.test-tmp'
        parent.mkdir(exist_ok=True)
        self.temporary=tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(self.temporary.cleanup)
        self.path=Path(self.temporary.name)/'synthetic.elf'

    def write(self, data=None):
        data=make_elf() if data is None else data
        self.path.write_bytes(data)
        return data

    def error(self, code, action):
        with self.assertRaises(ELFError) as caught:action()
        self.assertEqual(caught.exception.code,code)
        return caught.exception

    def test_metadata_and_cached_streaming_hash_use_one_descriptor(self):
        body=self.write(make_elf(file_size=2*MAX_READ+123))
        with ELFFile(self.path) as elf:
            self.assertEqual((elf.architecture,elf.machine,elf.elf_type,elf.size,elf.image_vaddr),('x86_64',62,'ET_DYN',len(body),0))
            segment=elf.segments[0]
            self.assertTrue(segment.readable);self.assertTrue(segment.executable);self.assertFalse(segment.writable)
            self.assertEqual(segment.offset,0)
            with patch.object(elf,'_read_at',wraps=elf._read_at) as reads:
                digest=elf.sha256()
                self.assertEqual(len(reads.call_args_list),3)
                self.assertTrue(all(c.args[1] <= MAX_READ for c in reads.call_args_list))
                reads.reset_mock()
                self.assertEqual(elf.sha256(),digest)
                reads.assert_not_called()
            self.assertEqual(digest,hashlib.sha256(body).hexdigest())
        self.error('elf_not_open',elf.sha256)

    def test_rva_uses_segment_file_offset_and_checks_permissions(self):
        body=self.write(make_elf([dict(offset=0,vaddr=0,filesz=0x100,flags=4),
                                  dict(offset=0x300,vaddr=0x200,filesz=0x100,flags=5)]))
        with ELFFile(self.path) as elf:
            self.assertEqual(elf.rva_to_offset(0x210,16),0x310)
            self.assertEqual(elf.read_rva(0x210,16),body[0x310:0x320])
            self.error('rva_not_executable',lambda:elf.read_rva(0x10,16))
            self.assertEqual(elf.read_rva(0x10,16,executable=False),body[0x10:0x20])
            self.error('rva_unmapped',lambda:elf.read_rva(0x180,16))

    def test_bss_and_ranges_crossing_file_backing_are_rejected(self):
        self.write()
        with ELFFile(self.path) as elf:
            for start,size in ((0x600,1),(0x5f8,16)):
                self.error('rva_not_file_backed',lambda:elf.rva_to_offset(start,size))
            self.error('rva_unmapped',lambda:elf.rva_to_offset(0x800))
            for rva,size in ((True,1),(-1,1),(0,0),(0,False),((1<<64)-1,2)):
                self.error('invalid_rva_range',lambda:elf.rva_to_offset(rva,size))
            self.error('rva_read_limit',lambda:elf.read_rva(0,MAX_READ+1))

    def test_cross_segment_and_overlapping_loads_never_choose_arbitrarily(self):
        self.write(make_elf([dict(offset=0,vaddr=0,filesz=0x300),dict(offset=0x300,vaddr=0x300,filesz=0x100)]))
        with ELFFile(self.path) as elf:
            self.error('rva_crosses_segment',lambda:elf.read_rva(0x2f0,32))
        self.write(make_elf([dict(offset=0,vaddr=0,filesz=0x400),dict(offset=0x300,vaddr=0x300,filesz=0x200)]))
        with ELFFile(self.path) as elf:
            for start in (0x310,0x2f8):
                self.error('ambiguous_load_range',lambda:elf.read_rva(start,16))

    def test_empty_load_segment_does_not_cover_or_ambiguate_code_bytes(self):
        body=self.write(make_elf([dict(offset=0,vaddr=0,filesz=0x600),
                                  dict(offset=0,vaddr=0x200,filesz=0,memsz=0)]))
        with ELFFile(self.path) as elf:
            self.assertEqual(len(elf.segments),2)
            self.assertEqual(elf.read_rva(0x1f8,16),body[0x1f8:0x208])
            self.assertEqual(elf.rva_to_offset(0x1f8,16),0x1f8)
            self.assertEqual(list(elf.iter_executable_chunks()),[(0,body)])

    def test_nonzero_image_is_described_but_never_guessed_for_profile_rvas(self):
        body=self.write(make_elf([dict(offset=0,vaddr=0x400000,filesz=0x600,flags=5,align=0x1000)],elf_type=2))
        with ELFFile(self.path) as elf:
            self.assertEqual(elf.elf_type,'ET_EXEC')
            self.assertEqual(elf.image_vaddr,0x400000)
            self.assertEqual(elf.sha256(),hashlib.sha256(body).hexdigest())
            self.error('nonzero_image_vaddr',lambda:elf.rva_to_offset(0,1))
            self.error('nonzero_image_vaddr',lambda:elf.read_rva(0,1))
            self.error('nonzero_image_vaddr',lambda:list(elf.iter_executable_chunks()))

    def test_machine_and_elf_type_are_reported_without_claiming_compatibility(self):
        for machine,architecture,kind,name in ((183,'aarch64',3,'ET_DYN'),(0xffff,'unknown',0xffff,'ET_UNKNOWN')):
            self.write(make_elf(machine=machine,elf_type=kind))
            with ELFFile(self.path) as elf:
                self.assertEqual((elf.architecture,elf.elf_type),(architecture,name))

    def test_executable_chunks_preserve_boundary_overlap_and_skip_data(self):
        body=self.write(make_elf([dict(offset=0,vaddr=0,filesz=0x100,flags=4),
                                  dict(offset=0x200,vaddr=0x200,filesz=23,flags=5)]))
        with ELFFile(self.path) as elf:
            chunks=list(elf.iter_executable_chunks(chunk_size=10,overlap=3))
            self.assertEqual([rva for rva,_ in chunks],[0x200,0x207,0x20e])
            for rva,data in chunks:self.assertEqual(data,body[rva:rva+len(data)])
            self.assertTrue(all(len(data)<=10 for _,data in chunks))
            for size,overlap in ((0,0),(True,0),(MAX_READ+1,0),(10,10),(10,-1),(10,True)):
                self.error('invalid_chunk_options',lambda:list(elf.iter_executable_chunks(size,overlap)))

    def test_header_validation_rejects_truncation_bad_class_endian_and_large_tables(self):
        good=make_elf()
        variants=[(b'not ELF','truncated_elf_header')]
        for offset,body,code in ((0,b'nope','not_elf'),(4,b'\x01','unsupported_elf_class'),
                (5,b'\x02','unsupported_endianness'),(6,b'\x02','unsupported_elf_version'),
                (52,struct.pack('<H',63),'invalid_elf_header'),(54,struct.pack('<H',55),'invalid_program_headers'),
                (56,struct.pack('<H',4097),'too_many_program_headers'),(56,b'\xff\xff','extended_program_headers_unsupported'),
                (56,b'\0\0','missing_load_segments'),(32,struct.pack('<Q',(1<<64)-64),'invalid_program_headers')):
            changed=bytearray(good);changed[offset:offset+len(body)]=body
            variants.append((changed,code))
        for body,code in variants:
            with self.subTest(code=code):
                self.write(body)
                instance=ELFFile(self.path)
                self.error(code,instance.__enter__)
                self.assertIsNone(instance._stream)

    def test_load_validation_rejects_bad_file_memory_and_alignment_ranges(self):
        for segment,code in ((dict(filesz=0x700,memsz=0x800),'invalid_load_range'),
                (dict(filesz=0x300,memsz=0x200),'invalid_load_range'),
                (dict(offset=0x500,filesz=0x200),'invalid_load_range'),
                (dict(vaddr=(1<<64)-8,filesz=16),'invalid_load_range'),
                (dict(filesz=0x200,align=3),'invalid_load_alignment'),
                (dict(offset=1,vaddr=0,filesz=0x200,align=0x1000),'invalid_load_alignment'),
                (dict(type=4,filesz=0x200),'missing_load_segments'),
                (dict(filesz=0,memsz=0),'empty_load_segments')):
            self.write(make_elf([segment]))
            self.error(code,lambda:ELFFile(self.path).__enter__())

    def test_modified_file_invalidates_even_a_cached_hash(self):
        self.write()
        with self.assertRaises(ELFError) as closing:
            with ELFFile(self.path) as elf:
                elf.sha256()
                with self.path.open('ab') as stream:stream.write(b'change')
                self.error('elf_changed',elf.sha256)
                self.error('elf_changed',lambda:elf.read_rva(0,1))
                self.error('elf_changed',elf.check_unchanged)
        self.assertEqual(closing.exception.code,'elf_changed')
        self.assertIsNone(elf._stream)

    def test_distinct_windows_path_ctime_is_kept_as_its_own_snapshot(self):
        self.write()
        real_stat=os.stat
        adjustment=[100]
        def path_stat(path,*args,**kwargs):
            info=real_stat(path,*args,**kwargs)
            if Path(path)==self.path and kwargs.get('follow_symlinks') is False:
                return SimpleNamespace(st_dev=info.st_dev,st_ino=info.st_ino,st_mode=info.st_mode,st_size=info.st_size,
                    st_mtime_ns=info.st_mtime_ns,st_ctime_ns=info.st_ctime_ns+adjustment[0])
            return info
        with patch('wechat_linux.analysis.elf.os.stat',side_effect=path_stat):
            with self.assertRaises(ELFError) as closing:
                with ELFFile(self.path) as elf:
                    self.assertEqual(elf.sha256(),hashlib.sha256(self.path.read_bytes()).hexdigest())
                    adjustment[0]=101
                    self.error('elf_changed',elf.check_unchanged)
            self.assertEqual(closing.exception.code,'elf_changed')

    def test_fd_ctime_changes_are_detected_independently_of_path_stat(self):
        self.write()
        real_fstat=os.fstat
        adjustment=[0]
        def fd_stat(fd):
            info=real_fstat(fd)
            return SimpleNamespace(st_dev=info.st_dev,st_ino=info.st_ino,st_mode=info.st_mode,st_size=info.st_size,
                                   st_mtime_ns=info.st_mtime_ns,st_ctime_ns=info.st_ctime_ns+adjustment[0])
        with patch('wechat_linux.analysis.elf.os.fstat',side_effect=fd_stat):
            with self.assertRaises(ELFError) as closing:
                with ELFFile(self.path) as elf:
                    elf.sha256()
                    adjustment[0]=1
                    self.error('elf_changed',elf.sha256)
            self.assertEqual(closing.exception.code,'elf_changed')

    @unittest.skipIf(os.name=='nt','POSIX rename-over-open and FIFO semantics')
    def test_path_replacement_and_nonregular_input_are_rejected(self):
        body=self.write()
        with self.assertRaises(ELFError) as closing:
            with ELFFile(self.path) as elf:
                replacement=self.path.with_suffix('.new')
                replacement.write_bytes(body)
                os.replace(replacement,self.path)
                self.error('elf_changed',elf.sha256)
        self.assertEqual(closing.exception.code,'elf_changed')
        fifo=self.path.with_suffix('.fifo');os.mkfifo(fifo)
        self.error('unsafe_elf_path',lambda:ELFFile(fifo).__enter__())


if __name__=='__main__':unittest.main()
