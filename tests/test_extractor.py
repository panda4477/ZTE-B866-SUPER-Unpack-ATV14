#!/usr/bin/env python3
"""Small synthetic liblp regression tests (not a test with real phone ROM)."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'extract_super.py'


def make_super(path, corrupt_primary=False, erofs=False):
    size = 1024 * 1024
    data = bytearray(size)
    slot_bytes = 4096
    slot_count = 2
    geo = bytearray(52)
    struct.pack_into('<II', geo, 0, 0x616C4467, 52)
    struct.pack_into('<III', geo, 40, slot_bytes, slot_count, 4096)
    geo[8:40] = hashlib.sha256(geo).digest()
    data[4096:4096 + len(geo)] = geo
    data[8192:8192 + len(geo)] = geo

    def partition(name, first_extent):
        return struct.pack('<36sIIII', name.encode('ascii'), 1, first_extent, 1, 0)

    tables = (partition('system_a', 0) + partition('vendor_a', 1) +
              struct.pack('<QIQI', 16, 0, 128, 0) +
              struct.pack('<QIQI', 8, 0, 144, 0) +
              struct.pack('<QIIQ36sI', 128, 0, 0, size, b'super', 0))
    header = bytearray(128)
    struct.pack_into('<IHHII', header, 0, 0x414C5030, 10, 0, 128, 0)
    struct.pack_into('<I', header, 44, len(tables))
    header[48:80] = hashlib.sha256(tables).digest()
    struct.pack_into('<III', header, 80, 0, 2, 52)
    struct.pack_into('<III', header, 92, 104, 2, 24)
    struct.pack_into('<III', header, 104, 152, 0, 48)
    struct.pack_into('<III', header, 116, 152, 1, 64)
    header[12:44] = hashlib.sha256(header).digest()
    blob = header + tables
    for index in range(slot_count * 2):
        pos = 12288 + index * slot_bytes
        data[pos:pos + len(blob)] = blob
    if corrupt_primary:
        data[12288 + 20] ^= 1
    data[128 * 512:144 * 512] = b'S' * (16 * 512)
    data[144 * 512:152 * 512] = b'V' * (8 * 512)
    if erofs:
        block = 128 * 512 + 1024
        data[block:block + 4] = b'\xe2\xe1\xf5\xe0'
        data[block + 12] = 12
        struct.pack_into('<I', data, block + 36, 2)
    path.write_bytes(data)
    return data


class ExtractorTests(unittest.TestCase):
    def test_raw_extraction_and_sha(self):
        with tempfile.TemporaryDirectory() as root:
            p = Path(root) / 'super.img'
            raw = make_super(p)
            result = subprocess.run([sys.executable, str(SCRIPT), str(p), '--images-only'],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            outputs = list(Path(root).glob('super_unpacked_*'))
            self.assertEqual(len(outputs), 1)
            output = outputs[0]
            self.assertEqual((output / 'partitions/system_a.img').read_bytes(),
                             raw[128 * 512:144 * 512])
            self.assertEqual((output / 'partitions/vendor_a.img').read_bytes(),
                             b'V' * (8 * 512))
            report = json.loads((output / 'extraction_report.json').read_text())
            self.assertEqual(report['metadata_slot'], 0)
            self.assertEqual(len(report['images']), 2)

    def test_list_has_no_output_and_input_unchanged(self):
        with tempfile.TemporaryDirectory() as root:
            p = Path(root) / 'super.img'
            raw = make_super(p)
            result = subprocess.run([sys.executable, str(SCRIPT), str(p), '--list'],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(json.loads(result.stdout)), 2)
            self.assertEqual(p.read_bytes(), raw)
            self.assertEqual(list(Path(root).glob('super_unpacked_*')), [])

    def test_fallback_backup_metadata(self):
        with tempfile.TemporaryDirectory() as root:
            p = Path(root) / 'super.img'
            make_super(p, corrupt_primary=True)
            result = subprocess.run([sys.executable, str(SCRIPT), str(p), '--slot', '0', '--images-only'],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('Using metadata slot 0', result.stdout)

    def test_repeat_run_gives_different_folders(self):
        with tempfile.TemporaryDirectory() as root:
            p = Path(root) / 'super.img'
            make_super(p)
            for _ in range(2):
                result = subprocess.run([sys.executable, str(SCRIPT), str(p), '--images-only'],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(list(Path(root).glob('super_unpacked_*'))), 2)

    def test_reject_existing_output(self):
        with tempfile.TemporaryDirectory() as root:
            p = Path(root) / 'super.img'
            make_super(p)
            result = subprocess.run([sys.executable, str(SCRIPT), str(p), '-o', root],
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('already exists', result.stderr)

    def test_default_system_unpack_with_fake_erofs_tool(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            p = root / 'super.img'
            make_super(p, erofs=True)
            bindir = root / 'bin'
            bindir.mkdir()
            fake = bindir / 'fsck.erofs'
            fake.write_text('#!/bin/sh\ncase "$1" in --extract=*) x="${1#--extract=}";; *) exit 2;; esac\n'
                            'mkdir -p "$x/system"\necho "ro.product=1" > "$x/system/build.prop"\n')
            fake.chmod(0o755)
            env = os.environ.copy()
            env['PATH'] = str(bindir) + os.pathsep + env['PATH']
            result = subprocess.run([sys.executable, str(SCRIPT), str(p)],
                                    capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            outputs = list(root.glob('super_unpacked_*'))
            self.assertTrue((outputs[0] / 'system/system/build.prop').is_file())

    def test_all_erofs_unpack_folders(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            p = root / 'super.img'
            make_super(p, erofs=True)
            fake_dir = root / 'bin'
            fake_dir.mkdir()
            fake = fake_dir / 'fsck.erofs'
            fake.write_text('#!/bin/sh\nx="${1#--extract=}"\nmkdir -p "$x"\necho ok > "$x/file"\n')
            fake.chmod(0o755)
            env = os.environ.copy()
            env['PATH'] = str(fake_dir) + os.pathsep + env['PATH']
            result = subprocess.run([sys.executable, str(SCRIPT), str(p), '--all-erofs'],
                                    capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            outputs = list(root.glob('super_unpacked_*'))
            self.assertTrue((outputs[0] / 'files/system_a/file').is_file())

    def test_sparse_conversion_using_stub_converter(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            raw_path = root / 'source.raw'
            make_super(raw_path)
            sparse = root / 'super.img'
            sparse.write_bytes(b'\x3a\xff\x26\xedFAKE')
            bindir = root / 'bin'
            bindir.mkdir()
            converter = bindir / 'simg2img'
            converter.write_text('#!/bin/sh\ncp "$TEST_RAW_IMAGE" "$2"\n')
            converter.chmod(0o755)
            env = os.environ.copy()
            env['PATH'] = str(bindir) + os.pathsep + env['PATH']
            env['TEST_RAW_IMAGE'] = str(raw_path)
            result = subprocess.run([sys.executable, str(SCRIPT), str(sparse), '--images-only'],
                                    capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('Android sparse detected', result.stdout)
            outputs = list(root.glob('super_unpacked_*'))
            self.assertTrue((outputs[0] / 'partitions/system_a.img').is_file())
            self.assertFalse((outputs[0] / 'super_converted_raw.img').exists())

    def test_invalid_image_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            p = Path(root) / 'super.img'
            p.write_bytes(b'bad' * 100)
            result = subprocess.run([sys.executable, str(SCRIPT), str(p), '--list'],
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('ERROR', result.stderr)


if __name__ == '__main__':
    unittest.main()
