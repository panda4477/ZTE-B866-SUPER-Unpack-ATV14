#!/usr/bin/env python3
"""Extract Android liblp super images on Linux / Ubuntu / WSL (Python 3.8+).

Run: python3 extract_super.py ./super.img
Optional: --list, --slot N, --images-only, --all-erofs, --output DIR

For Android sparse super images install simg2img; for EROFS directory extraction
install erofs-utils. Sources are opened read-only; outputs go into a new
super_unpacked_YYYY-MM-DD_HH-MM-SS directory next to the input image.
"""

import argparse
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile

CHUNK = 8 * 1024 * 1024
SECTOR = 512
SPARSE_MAGIC = b'\x3a\xff\x26\xed'
SYSTEM_RE = re.compile(r'system(?:_[ab])?\Z')
SAFE_PART_RE = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_-]*\Z')


def check(ok, message):
    if not ok:
        raise ValueError(message)


def read_exact(file, offset, amount):
    file.seek(offset)
    data = file.read(amount)
    check(len(data) == amount, 'Unexpected end of image at offset %d' % offset)
    return data


def digest_file(path):
    checksum = hashlib.sha256()
    with path.open('rb') as stream:
        for data in iter(lambda: stream.read(CHUNK), b''):
            checksum.update(data)
    return checksum.hexdigest()


def geometry(file):
    errors = []
    for offset in (4096, 8192):
        try:
            raw = read_exact(file, offset, 4096)
            magic, size = struct.unpack_from('<II', raw)
            check(magic == 0x616C4467 and 52 <= size <= 4096, 'Bad geometry magic/size')
            sanitized = raw[:8] + bytes(32) + raw[40:size]
            check(hashlib.sha256(sanitized).digest() == raw[8:40], 'Geometry checksum mismatch')
            max_size, slots, logical_block_size = struct.unpack_from('<III', raw, 40)
            check(512 <= max_size <= 16 * 1024 * 1024 and max_size % 512 == 0,
                  'Invalid metadata slot size')
            check(1 <= slots <= 32 and logical_block_size >= 512,
                  'Invalid metadata slot count or logical block size')
            return max_size, slots
        except (ValueError, struct.error) as exc:
            errors.append(str(exc))
    raise ValueError('Both geometry copies invalid: ' + '; '.join(errors))


def metadata_at(file, offset, maximum, image_size):
    prefix = read_exact(file, offset, 128)
    magic, major, minor, header_size = struct.unpack_from('<IHHI', prefix)
    check(magic == 0x414C5030 and major == 10 and minor <= 2,
          'Unsupported or invalid liblp metadata header')
    check(128 <= header_size <= maximum, 'Invalid metadata header size')
    header = read_exact(file, offset, header_size)
    check(hashlib.sha256(header[:12] + bytes(32) + header[44:]).digest() == header[12:44],
          'Metadata header SHA256 mismatch')
    table_size = struct.unpack_from('<I', header, 44)[0]
    check(header_size + table_size <= maximum, 'Metadata tables exceed slot size')
    tables = read_exact(file, offset + header_size, table_size)
    check(hashlib.sha256(tables).digest() == header[48:80],
          'Metadata tables SHA256 mismatch')

    def table(position, fmt):
        start, count, stride = struct.unpack_from('<III', header, position)
        check(stride >= struct.calcsize(fmt) and start <= table_size and
              count * stride <= table_size - start, 'Invalid liblp table range')
        return [struct.unpack_from(fmt, tables, start + i * stride) for i in range(count)]

    partitions = table(80, '<36sIIII')
    extents = table(92, '<QIQI')
    devices = table(116, '<QIIQ36sI')
    check(len(devices) == 1, 'Only single-device super images are supported')
    first_logical_sector = devices[0][0]
    check(devices[0][3] <= image_size, 'Block device is larger than the input image')
    result = []
    used = set()
    for raw_name, attributes, first, count, group in partitions:
        name = raw_name.split(b'\x00', 1)[0].decode('ascii')
        check(SAFE_PART_RE.fullmatch(name), 'Unsafe partition name: ' + repr(name))
        check(name not in used, 'Repeated partition name: ' + name)
        used.add(name)
        check(first <= len(extents) and count <= len(extents) - first,
              'Invalid extent range for ' + name)
        selected = extents[first:first + count]
        for sectors, kind, sector, dev_index in selected:
            check(kind in (0, 1), 'Unsupported extent type in ' + name)
            if kind == 0:
                check(dev_index == 0 and sector >= first_logical_sector and
                      sector <= image_size // SECTOR and sectors <= image_size // SECTOR - sector,
                      'Extent outside super image in ' + name)
        result.append(dict(name=name, size=sum(ext[0] * SECTOR for ext in selected),
                           extents=selected, attributes=attributes, group=group))
    return result


def logical_read(file, partition, offset, size):
    check(offset >= 0 and size >= 0 and size <= partition['size'] - offset,
          'Logical read outside partition')
    out = bytearray()
    logical_offset = 0
    remaining = size
    for sectors, kind, sector, _device in partition['extents']:
        length = sectors * SECTOR
        if remaining and offset < logical_offset + length:
            inside = max(offset - logical_offset, 0)
            take = min(remaining, length - inside)
            out.extend(read_exact(file, sector * SECTOR + inside, take)
                       if kind == 0 else bytes(take))
            offset += take
            remaining -= take
        logical_offset += length
    check(remaining == 0, 'Logical partition read incomplete')
    return bytes(out)


def list_slots(file, image_size):
    maximum, slots_count = geometry(file)
    entries = []
    for slot in range(slots_count):
        partitions = None
        errors = []
        for copy_number in (slot, slots_count + slot):
            try:
                partitions = metadata_at(file, 12288 + copy_number * maximum,
                                         maximum, image_size)
                break
            except (ValueError, UnicodeError, struct.error) as exc:
                errors.append(str(exc))
        if partitions is None:
            entries.append(dict(slot=slot, usable=False, issues=errors, partitions=[]))
            continue
        issues = []
        for part in partitions:
            if not part['size']:
                continue
            if part['name'].endswith('-cow'):
                if part['size'] < 16:
                    issues.append(part['name'] + ': invalid snapshot storage')
                    continue
                head = logical_read(file, part, 0, 16)
                magic, valid, version, chunk_sectors = struct.unpack('<IIII', head)
                chunk = chunk_sectors * SECTOR
                if (magic == 0x70416e53 and valid == 1 and version == 1 and
                        SECTOR <= chunk <= part['size'] - 16):
                    _old, new = struct.unpack('<QQ', logical_read(file, part, chunk, 16))
                    if new:
                        issues.append(part['name'] + ': unapplied snapshot overlay is not supported')
                else:
                    issues.append(part['name'] + ': unknown snapshot storage, cannot safely merge')
                continue
            if part['size'] >= 2048:
                superblock = logical_read(file, part, 1024, 1024)
                if superblock[:4] == b'\xe2\xe1\xf5\xe0':
                    part['filesystem'] = 'erofs'
                    blockbits = superblock[12]
                    if 9 <= blockbits <= 16:
                        fs_size = struct.unpack_from('<I', superblock, 36)[0] << blockbits
                        if fs_size > part['size']:
                            issues.append(part['name'] + ': EROFS filesystem exceeds partition')
                elif superblock[56:58] == b'\x53\xef':
                    part['filesystem'] = 'ext4'
        entries.append(dict(slot=slot, usable=not issues,
                            issues=issues, partitions=partitions))
    return entries


def partition_chunks(file, partition):
    for sectors, kind, sector, _device in partition['extents']:
        size = sectors * SECTOR
        if kind == 0:
            file.seek(sector * SECTOR)
        while size:
            amount = min(size, CHUNK)
            block = file.read(amount) if kind == 0 else bytes(amount)
            check(len(block) == amount, 'Truncated data in partition ' + partition['name'])
            yield block
            size -= amount


def write_partition(file, partition, output):
    images_dir = output / 'partitions'
    images_dir.mkdir(exist_ok=True)
    target = images_dir / (partition['name'] + '.img')
    partial = target.with_suffix('.img.partial')
    checksum = hashlib.sha256()
    try:
        with partial.open('xb') as dest:
            for chunk in partition_chunks(file, partition):
                dest.write(chunk)
                checksum.update(chunk)
        check(digest_file(partial) == checksum.hexdigest(),
              'SHA256 verification failed: ' + str(partial))
        partial.rename(target)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    print('Extracted', target.name, '(%d bytes, SHA256 verified)' % partition['size'], flush=True)
    return dict(name=partition['name'], size=partition['size'],
                file=str(target.relative_to(output)), sha256=checksum.hexdigest(),
                filesystem=partition.get('filesystem', 'unknown'))


def unpack_erofs(info, output, subfolder):
    tool = shutil.which('fsck.erofs')
    check(tool is not None,
          'Cannot unpack EROFS directory: fsck.erofs missing. Run bash install_tools.sh '
          'or use --images-only (partition .img files have been extracted).')
    source = output / info['file']
    dest = output / subfolder
    partial = output / (subfolder + '.partial')
    check(not dest.exists() and not partial.exists(), 'EROFS directory already exists')
    log = output / (subfolder.replace('/', '_') + '_erofs.log')
    partial.parent.mkdir(parents=True, exist_ok=True)
    print('Unpacking EROFS', info['name'], '...', flush=True)
    # --extract=PATH is available on modern versions of erofs-utils.
    # Older versions supporting only --extract cannot unpack into a directory.
    with log.open('w', encoding='utf-8') as dest_log:
        completed = subprocess.run([tool, '--extract=' + str(partial), str(source)],
                                   stdout=dest_log, stderr=subprocess.STDOUT,
                                   check=False)
    if completed.returncode != 0:
        raise RuntimeError('fsck.erofs failed for %s (exit %d); see %s. '
                           'Some older versions do not support --extract=DIR; '
                           'the .img file is still available.' %
                           (info['name'], completed.returncode, log))
    check(partial.is_dir(), 'fsck.erofs did not produce a directory: ' + str(partial))
    check(any(partial.iterdir()), 'EROFS extraction produced no files: ' + info['name'])
    partial.rename(dest)
    print('Unpacked directory', dest, flush=True)
    return str(dest.relative_to(output))


def create_new_output(image, requested):
    if requested is not None:
        path = Path(requested).expanduser().absolute()
        check(not path.exists() and not path.is_symlink(),
              'Output folder already exists; choose a new --output path: ' + str(path))
        path.mkdir(parents=True, exist_ok=False)
        return path
    stamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    base = image.parent / ('super_unpacked_' + stamp)
    for counter in range(1000):
        candidate = base if counter == 0 else Path(str(base) + '_%03d' % counter)
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            continue
    raise RuntimeError('Unable to create a unique dated output directory')


@contextmanager
def accessible_raw(image, temporary_directory):
    with image.open('rb') as stream:
        is_sparse = stream.read(4) == SPARSE_MAGIC
    if not is_sparse:
        yield image
        return
    simg2img = shutil.which('simg2img')
    check(simg2img is not None,
          'This is an Android sparse image. Install simg2img: bash install_tools.sh')
    converted = temporary_directory / 'super_converted_raw.img'
    print('Android sparse detected. Converting to raw with simg2img...', flush=True)
    try:
        subprocess.run([simg2img, str(image), str(converted)], check=True)
        yield converted
    finally:
        converted.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('image', nargs='?', default='super.img',
                        help='Path to super.img; default: ./super.img')
    parser.add_argument('--list', action='store_true', help='Display metadata, do not extract')
    parser.add_argument('--slot', type=int, default=None, help='Metadata slot number, e.g. 0 or 1')
    parser.add_argument('--images-only', action='store_true', help='Only export partition .img files')
    parser.add_argument('--all-erofs', action='store_true',
                        help='Also unpack every EROFS partition to files/PARTITION/')
    parser.add_argument('-o', '--output', help='Fresh output directory; must not already exist')
    args = parser.parse_args()
    image = Path(args.image).expanduser().resolve(strict=True)
    check(image.is_file(), 'Input must be an image file')
    if args.list:
        with tempfile.TemporaryDirectory(prefix='super_list_', dir=image.parent) as tmp:
            with accessible_raw(image, Path(tmp)) as raw:
                with raw.open('rb') as stream:
                    slots = list_slots(stream, raw.stat().st_size)
                print(json.dumps(slots, indent=2))
        return

    output = create_new_output(image, args.output)
    print('Input:', image, flush=True)
    print('Output:', output, flush=True)
    try:
        with accessible_raw(image, output) as raw:
            with raw.open('rb') as stream:
                slots = list_slots(stream, raw.stat().st_size)
                for slot in slots:
                    print('Slot %d: %s' % (slot['slot'], 'OK' if slot['usable'] else
                          '; '.join(slot['issues'])), flush=True)
                eligible = [s for s in slots if s['usable'] and
                            (args.slot is None or s['slot'] == args.slot)]
                check(eligible, 'No usable metadata slot. Run --list for details.')
                selected = eligible[0]
                partitions = [p for p in selected['partitions'] if p['size'] > 0
                              and not p['name'].endswith('-cow')]
                required = sum(p['size'] for p in partitions)
                available = shutil.disk_usage(output).free
                check(required + 64 * 1024 * 1024 <= available,
                      'Not enough disk space for partitions (need about %d bytes)' % required)
                print('Using metadata slot', selected['slot'], flush=True)
                records = [write_partition(stream, p, output) for p in partitions]
        report = dict(source=str(image), source_size=image.stat().st_size,
                      metadata_slot=selected['slot'], images=records,
                      note='Raw logical partitions. Snapshot -cow overlays are not merged; '
                           'ext4 filesystems are exported as images, not directory trees.')
        (output / 'extraction_report.json').write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        if not args.images_only:
            targets = ([info for info in records if info['filesystem'] == 'erofs']
                       if args.all_erofs else
                       [info for info in records if info['filesystem'] == 'erofs'
                        and SYSTEM_RE.fullmatch(info['name'])])
            if not targets:
                print('No EROFS system partition to unpack; .img files are ready.', flush=True)
            for info in targets:
                subfolder = ('files/' + info['name']) if args.all_erofs else 'system'
                folder = unpack_erofs(info, output, subfolder)
                report.setdefault('erofs_directories', {})[info['name']] = folder
                (output / 'extraction_report.json').write_text(
                    json.dumps(report, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    except BaseException:
        print('Partial outputs (if any) remain in:', output, file=sys.stderr)
        raise
    print('DONE:', output, flush=True)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print('ERROR:', error, file=sys.stderr)
        sys.exit(1)
