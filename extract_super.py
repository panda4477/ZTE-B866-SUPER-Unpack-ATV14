#!/usr/bin/env python3
"""Extract this raw Android super.img beside this script (Python 3.8+).

Usage: python extract_super.py [--list] [--slot 0|1|2] [--images-only]
Partition extraction uses the Python standard library. Unpacking EROFS files
requires fsck.erofs from erofs-utils, either locally or in Windows WSL.
All outputs stay in this script's directory. The input is opened read-only.
Format reference:
https://android.googlesource.com/platform/system/core/+/refs/heads/main/fs_mgr/liblp/include/liblp/metadata_format.h
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys

BASE = Path(__file__).resolve().parent
SOURCE = BASE / 'super.img'
CHUNK = 8 * 1024 * 1024


def need(condition, message):
    if not condition:
        raise ValueError(message)


def exact(stream, offset, size):
    stream.seek(offset)
    data = stream.read(size)
    need(len(data) == size, 'Unexpected end of image at byte %d' % offset)
    return data


def sha_file(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for data in iter(lambda: stream.read(CHUNK), b''):
            digest.update(data)
    return digest.hexdigest()


def output_path(name):
    path = BASE / name
    need(not path.is_symlink(), 'Output must not be a symlink: ' + str(path))
    need(BASE in path.resolve().parents, 'Output must stay beside the script')
    return path


def geometry(stream):
    need(exact(stream, 0, 4) != b'\x3a\xff\x26\xed',
         'This script accepts raw super.img; Android sparse input is not supported.')
    errors = []
    for offset in (4096, 8192):
        try:
            data = exact(stream, offset, 4096)
            magic, size = struct.unpack_from('<II', data)
            need(magic == 0x616C4467 and 52 <= size <= 4096, 'Bad geometry')
            check = data[:8] + bytes(32) + data[40:size]
            need(hashlib.sha256(check).digest() == data[8:40], 'Bad geometry checksum')
            maximum, count, block = struct.unpack_from('<III', data, 40)
            need(512 <= maximum <= 16 * 1024 * 1024 and maximum % 512 == 0,
                 'Invalid metadata size')
            need(1 <= count <= 32 and block >= 512, 'Invalid slot count/block size')
            return maximum, count
        except ValueError as error:
            errors.append(str(error))
    raise ValueError('; '.join(errors))


def metadata_at(stream, offset, maximum, image_size):
    prefix = exact(stream, offset, 128)
    magic, major, minor, size = struct.unpack_from('<IHHI', prefix)
    need(magic == 0x414C5030 and major == 10 and minor <= 2, 'Unsupported LP header')
    need(128 <= size <= maximum, 'Bad header size')
    header = exact(stream, offset, size)
    need(hashlib.sha256(header[:12] + bytes(32) + header[44:]).digest() == header[12:44],
         'LP header checksum mismatch')
    table_size = struct.unpack_from('<I', header, 44)[0]
    need(size + table_size <= maximum, 'Tables exceed metadata area')
    tables = exact(stream, offset + size, table_size)
    need(hashlib.sha256(tables).digest() == header[48:80], 'LP table checksum mismatch')

    def table(position, fmt):
        start, count, stride = struct.unpack_from('<III', header, position)
        need(stride >= struct.calcsize(fmt) and start + count * stride <= table_size,
             'Invalid LP table bounds')
        return [struct.unpack_from(fmt, tables, start + i * stride) for i in range(count)]

    extents = table(92, '<QIQI')
    devices = table(116, '<QIIQ36sI')
    need(len(devices) == 1, 'Multi-device super images are not supported')
    partitions, names = [], set()
    for raw_name, attrs, first, count, group in table(80, '<36sIIII'):
        name = raw_name.split(b'\0', 1)[0].decode('ascii')
        need(re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_-]*', name) is not None,
             'Unsafe partition name')
        need(name not in names, 'Duplicate partition name')
        names.add(name)
        need(first + count <= len(extents), 'Invalid extent index')
        selected = extents[first:first + count]
        for sectors, kind, sector, device in selected:
            need(kind in (0, 1), 'Unsupported extent type')
            if kind == 0:
                need(device == 0 and sector >= devices[0][0] and
                     (sector + sectors) * 512 <= image_size, 'Extent outside source image')
        partitions.append(dict(name=name, size=sum(e[0] * 512 for e in selected),
                               extents=selected, attributes=attrs, group=group))
    return partitions


def logical_read(stream, part, offset, size):
    need(offset + size <= part['size'], 'Read exceeds logical partition')
    result = bytearray()
    logical = 0
    for sectors, kind, sector, device in part['extents']:
        length = sectors * 512
        if offset < logical + length and size:
            inside = max(0, offset - logical)
            take = min(size, length - inside)
            result.extend(exact(stream, sector * 512 + inside, take) if kind == 0 else bytes(take))
            offset += take
            size -= take
        logical += length
    need(size == 0, 'Incomplete partition read')
    return bytes(result)


def inspect_slots(stream):
    maximum, count = geometry(stream)
    image_size = SOURCE.stat().st_size
    slots = []
    for slot in range(count):
        partitions = None
        errors = []
        for copy in (slot, count + slot):
            try:
                partitions = metadata_at(stream, 12288 + copy * maximum, maximum, image_size)
                break
            except (ValueError, UnicodeError, struct.error) as error:
                errors.append(str(error))
        if partitions is None:
            slots.append(dict(slot=slot, usable=False, issues=errors, partitions=[]))
            continue
        issues = []
        systems = []
        for part in partitions:
            if not part['size']:
                continue
            if part['name'].endswith('-cow'):
                head = logical_read(stream, part, 0, 16)
                magic, valid, version, sectors = struct.unpack('<IIII', head)
                chunk = sectors * 512
                if (magic == 0x70416e53 and valid == 1 and version == 1 and
                        512 <= chunk <= part['size'] - 16):
                    old_chunk, new_chunk = struct.unpack('<QQ', logical_read(stream, part, chunk, 16))
                    part['snapshot_empty'] = new_chunk == 0
                    if new_chunk:
                        issues.append(part['name'] + ': pending snapshot overlay is not supported')
                else:
                    issues.append(part['name'] + ': unsupported snapshot storage')
                continue
            if re.fullmatch(r'system(?:_[ab])?', part['name']):
                systems.append(part)
            if part['size'] >= 2048:
                sb = logical_read(stream, part, 1024, 1024)
                if sb[:4] == b'\xe2\xe1\xf5\xe0':
                    need(9 <= sb[12] <= 16, 'Unsupported EROFS block size')
                    part['filesystem'] = 'erofs'
                    part['filesystem_size'] = struct.unpack_from('<I', sb, 36)[0] << sb[12]
                    if part['filesystem_size'] > part['size']:
                        issues.append(part['name'] + ': filesystem exceeds partition size')
        if not systems:
            issues.append('No nonempty system partition')
        slots.append(dict(slot=slot, usable=not issues, issues=issues, partitions=partitions))
    return slots


def stream_partition(stream, part):
    for sectors, kind, sector, device in part['extents']:
        left = sectors * 512
        if kind == 0:
            stream.seek(sector * 512)
        while left:
            amount = min(left, CHUNK)
            data = stream.read(amount) if kind == 0 else bytes(amount)
            need(len(data) == amount, 'Truncated source image')
            yield data
            left -= amount


def extract_image(stream, part):
    path = output_path('partitions/' + part['name'] + '.img')
    digest = hashlib.sha256()
    if path.exists():
        need(path.is_file() and path.stat().st_size == part['size'],
             'Existing image has a different size: ' + str(path))
        for data in stream_partition(stream, part):
            digest.update(data)
        need(sha_file(path) == digest.hexdigest(),
             'Existing image differs from source. Move it aside manually: ' + str(path))
        print('Verified existing', path.name, flush=True)
    else:
        partial = output_path('partitions/' + part['name'] + '.img.partial')
        need(not partial.exists(), 'Incomplete previous image exists: ' + str(partial))
        try:
            with partial.open('xb') as dest:
                for data in stream_partition(stream, part):
                    dest.write(data)
                    digest.update(data)
            need(sha_file(partial) == digest.hexdigest(), 'Output SHA-256 verification failed')
            partial.rename(path)
        except BaseException:
            if partial.exists():
                partial.unlink()
            raise
        print('Extracted and verified', path.name, part['size'], 'bytes', flush=True)
    return dict(name=part['name'], file='partitions/' + path.name,
                size=part['size'], sha256=digest.hexdigest(), filesystem=part.get('filesystem'))


def erofs_command(image, destination):
    native = shutil.which('fsck.erofs')
    args = ['--extract=' + str(destination), '--no-preserve', str(image)]
    if native:
        return [native] + args
    if os.name == 'nt' and shutil.which('wsl.exe'):
        def linux_path(path):
            return subprocess.check_output(['wsl.exe', '--exec', 'wslpath', '-a', '-u', str(path)],
                                           text=True).strip()
        return ['wsl.exe', '--exec', 'fsck.erofs', '--extract=' + linux_path(destination),
                '--no-preserve', linux_path(image)]
    raise RuntimeError('System files need fsck.erofs (erofs-utils) in Linux/WSL. '
                       'The partition .img files are already extracted. '
                       'On Ubuntu/WSL install erofs-utils, then run this script again.')


def extract_system(part, info):
    need(part.get('filesystem') == 'erofs', 'System filesystem is not EROFS')
    image = output_path(info['file'])
    destination = output_path('system')
    marker = output_path('system_extraction.json')
    if destination.exists():
        if marker.is_file():
            previous = json.loads(marker.read_text(encoding='utf-8'))
            need(previous.get('image_sha256') == info['sha256'], 'Existing system is from another image')
            need((destination / 'system' / 'build.prop').is_file() or
                 (destination / 'build.prop').is_file(), 'Existing system folder is incomplete')
            print('System directory already extracted; leaving it intact.', flush=True)
            return previous
        raise RuntimeError('System directory already exists without a completion record. '
                           'Move it aside manually before rerunning.')
    partial = output_path('system.partial')
    need(not partial.exists(), 'Incomplete system.partial exists; inspect/move it before rerunning')
    logfile = output_path('system_extract.log')
    command = erofs_command(image, partial)
    print('Checking and extracting EROFS system files...', flush=True)
    with logfile.open('w', encoding='utf-8') as log:
        subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
    need(partial.is_dir(), 'EROFS extractor did not create the destination')
    counts = dict(files=0, directories=0, symlinks=0, file_bytes=0)
    for root, dirs, files in os.walk(partial, followlinks=False):
        for name in dirs + files:
            item = Path(root) / name
            if item.is_symlink():
                counts['symlinks'] += 1
            elif item.is_dir():
                counts['directories'] += 1
            elif item.is_file():
                counts['files'] += 1
                counts['file_bytes'] += item.stat().st_size
    need(counts['files'] > 0, 'No system files were extracted')
    try:
        partial.rename(destination)
    except PermissionError:
        # Some Windows-backed directories reject renaming the directory itself.
        # Move its entries, preserving file contents and symbolic links.
        destination.mkdir()
        for child in list(partial.iterdir()):
            child.rename(destination / child.name)
        partial.rmdir()
    record = dict(partition=part['name'], image_sha256=info['sha256'], directory='system', **counts)
    marker.write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8')
    print('System extracted:', counts, flush=True)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--list', action='store_true', help='Read metadata without writing files')
    parser.add_argument('--slot', type=int, help='Select a metadata slot; default chooses a size-consistent slot')
    parser.add_argument('--images-only', action='store_true', help='Extract partition images without unpacking system')
    args = parser.parse_args()
    need(SOURCE.is_file(), 'Place super.img beside this script')
    with SOURCE.open('rb') as stream:
        slots = inspect_slots(stream)
        for entry in slots:
            print('Slot', entry['slot'], 'size-consistent' if entry['usable'] else '; '.join(entry['issues']))
        if args.list:
            print(json.dumps(slots, indent=2))
            return
        candidates = [entry for entry in slots if entry['usable'] and
                      (args.slot is None or entry['slot'] == args.slot)]
        need(candidates, 'No suitable metadata slot; use --list to inspect')
        selected = candidates[0]
        parts = [p for p in selected['partitions'] if p['size'] and not p['name'].endswith('-cow')]
        required = sum(p['size'] for p in parts if not (BASE / 'partitions' / (p['name'] + '.img')).exists())
        need(shutil.disk_usage(BASE).free > required + 64 * 1024 * 1024, 'Not enough disk space for images')
        directory = output_path('partitions')
        directory.mkdir(exist_ok=True)
        print('Selected slot', selected['slot'], flush=True)
        images = [extract_image(stream, part) for part in parts]
    report = dict(source='super.img', source_size=SOURCE.stat().st_size,
                  metadata_slot=selected['slot'], images=images, metadata=slots,
                  note='Empty partitions and OTA -cow storage are not exported. '
                       'Images contain the logical extents as stored; nonempty/unknown COW overlays are rejected.')
    report_path = output_path('extraction_report.json')
    report_path.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    if not args.images_only:
        system = next(p for p in parts if re.fullmatch(r'system(?:_[ab])?', p['name']))
        info = next(i for i in images if i['name'] == system['name'])
        report['system'] = extract_system(system, info)
        report_path.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print('Done. All outputs are in:', BASE, flush=True)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print('ERROR:', error, file=sys.stderr)
        sys.exit(1)
