"""Read/rebuild this project's PyInstaller archive without changing dependencies."""
from __future__ import annotations

import importlib.util
import marshal
from pathlib import Path
import struct
import zlib

COOKIE = struct.Struct("!8sIIII64s")
MAGIC = b"MEI\x0c\x0b\x0a\x0b\x0e"
TOC = struct.Struct("!iIIIBc")


def read_exe(path):
    raw = Path(path).read_bytes()
    at = raw.rfind(MAGIC)
    if at < 0:
        raise ValueError("Not a PyInstaller executable")
    magic, size, toc, length, version, library = COOKIE.unpack_from(raw, at)
    start = at + COOKIE.size - size
    if start <= 0 or at + COOKIE.size != len(raw):
        raise ValueError("Unexpected archive boundary or executable signature")
    entries, offset = {}, start + toc
    while offset < start + toc + length:
        size, pos, compressed_size, plain_size, compressed, kind = TOC.unpack_from(raw, offset)
        name = raw[offset + TOC.size:offset + size].rstrip(b"\0").decode()
        data = raw[start + pos:start + pos + compressed_size]
        data = zlib.decompress(data) if compressed else data
        if len(data) != plain_size:
            raise ValueError("Archive size mismatch: " + name)
        entries[name] = (kind, compressed, data)
        offset += size
    return raw[:start], version, library, entries


def read_pyz(data):
    if data[:4] != b"PYZ\0" or data[4:8] != importlib.util.MAGIC_NUMBER:
        raise ValueError("Use the exact Python version used by the portable executable")
    toc = dict(marshal.loads(data[struct.unpack("!i", data[8:12])[0]:]))
    return toc


def replace_pyz(data, replacements, *, removals=()):
    toc = read_pyz(data)
    output, new_toc = bytearray(data[:8] + b"\0\0\0\0"), []
    for name, (kind, pos, length) in toc.items():
        if name in removals:
            continue
        block = (zlib.compress(marshal.dumps(replacements.pop(name)))
                 if name in replacements else data[pos:pos + length])
        new_toc.append((name, (kind, len(output), len(block))))
        output.extend(block)
    for name, code in replacements.items():
        block = zlib.compress(marshal.dumps(code))
        new_toc.append((name, (0, len(output), len(block))))
        output.extend(block)
    struct.pack_into("!i", output, 8, len(output))
    output.extend(marshal.dumps(new_toc))
    read_pyz(output)
    return bytes(output)


def write_exe(path, bootloader, version, library, entries):
    archive, toc = bytearray(), bytearray()
    for name, (kind, compressed, data) in entries.items():
        block = zlib.compress(data) if compressed else data
        encoded = name.encode() + b"\0"
        size = (TOC.size + len(encoded) + 15) // 16 * 16
        toc.extend(TOC.pack(size, len(archive), len(block), len(data), compressed, kind))
        toc.extend(encoded.ljust(size - TOC.size, b"\0"))
        archive.extend(block)
    toc_at = len(archive)
    archive.extend(toc)
    archive.extend(COOKIE.pack(MAGIC, len(archive) + COOKIE.size,
                               toc_at, len(toc), version, library))
    Path(path).write_bytes(bootloader + archive)
    read_exe(path)
