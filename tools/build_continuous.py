"""Build a side-by-side EXE using the verified v2.0 bootloader/dependencies.

This incremental build updates every project module, removes deleted modules,
and writes versioned QML beside the existing portable dependencies. Use the
matching official Python 3.13.7 runtime. Existing releases are never replaced.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import json
import marshal
from pathlib import Path
import struct
import shutil
import sys
import tempfile
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from hdusign import __version__
from tools.frozen_archive import read_exe, read_pyz, replace_pyz, write_exe

BASE_SHA256 = "1ff68543727007a97f3746a30b851d76e04569007841e7d414ceb6d91194cafd"
UPDATE_CONTENT = "连堂课多账号与自动签到"
EXE_NAME = f"HDUSign-v{__version__}-{UPDATE_CONTENT}.exe"


def project_modules():
    """Compile all current sources so a changed module cannot be omitted."""
    result = {}
    for path in sorted((ROOT / "hdusign").rglob("*.py")):
        relative = path.relative_to(ROOT).with_suffix("")
        parts = relative.parts[:-1] if relative.name == "__init__" else relative.parts
        result[".".join(parts)] = path
    return result


def version_resource():
    def record(key, value=b"", children=(), text=False):
        data = bytearray(b"\0" * 6 + (key + "\0").encode("utf-16le"))
        data.extend(b"\0" * (-len(data) % 4))
        data.extend(value)
        data.extend(b"\0" * (-len(data) % 4))
        for child in children:
            data.extend(child)
            data.extend(b"\0" * (-len(data) % 4))
        struct.pack_into("<HHH", data, 0, len(data), len(value) // 2 if text else len(value), int(text))
        return bytes(data)
    a, b, c = (int(v) for v in __version__.split("."))
    fixed = struct.pack("<13I", 0xFEEF04BD, 0x10000, a << 16 | b, c << 16,
                        a << 16 | b, c << 16, 0x3F, 0, 0x40004, 1, 0, 0, 0)
    strings = {"CompanyName": "HDUSign", "FileDescription": "HDUSign " + UPDATE_CONTENT,
               "FileVersion": __version__ + ".0", "InternalName": "HDUSign",
               "OriginalFilename": EXE_NAME, "ProductName": "HDUSign",
               "ProductVersion": __version__}
    table = record("040904B0", children=[record(k, (v + "\0").encode("utf-16le"), text=True)
                                         for k, v in strings.items()], text=True)
    return record("VS_VERSION_INFO", fixed, (
        record("StringFileInfo", children=[table], text=True),
        record("VarFileInfo", children=[record("Translation", struct.pack("<HH", 0x409, 1200))], text=True)))


def update_version(bootloader):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.BeginUpdateResourceW.argtypes = [wintypes.LPCWSTR, wintypes.BOOL]
    kernel.BeginUpdateResourceW.restype = wintypes.HANDLE
    kernel.UpdateResourceW.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                      wintypes.WORD, ctypes.c_void_p, wintypes.DWORD]
    kernel.UpdateResourceW.restype = wintypes.BOOL
    kernel.EndUpdateResourceW.argtypes = [wintypes.HANDLE, wintypes.BOOL]
    kernel.EndUpdateResourceW.restype = wintypes.BOOL
    with tempfile.TemporaryDirectory(prefix="hdusign-build-") as tmp:
        stub = Path(tmp) / "bootloader.exe"
        stub.write_bytes(bootloader)
        handle = kernel.BeginUpdateResourceW(str(stub), False)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        resource = version_resource()
        buffer = ctypes.create_string_buffer(resource)
        if not kernel.UpdateResourceW(handle, 16, 1, 0, buffer, len(resource)):
            error = ctypes.get_last_error()
            kernel.EndUpdateResourceW(handle, True)
            raise ctypes.WinError(error)
        if not kernel.EndUpdateResourceW(handle, False):
            raise ctypes.WinError(ctypes.get_last_error())
        return stub.read_bytes()


def main():
    original = ROOT.parent / "HDUSign.exe"
    output = ROOT.parent / EXE_NAME
    qml_directory = ROOT.parent / "_internal" / ("qml-" + __version__)
    if output.exists() or qml_directory.exists():
        raise FileExistsError("A release or its versioned QML already exists; preserve it and select a new version")
    original_hash = hashlib.sha256(original.read_bytes()).hexdigest()
    if original_hash != BASE_SHA256:
        raise RuntimeError("The original EXE differs from the source-verified v2.0 base")
    if sys.version_info[:3] != (3, 13, 7):
        raise RuntimeError("Build with Python 3.13.7, matching the original executable")
    bootloader, version, library, entries = read_exe(original)
    pyz_name = next(name for name, (kind, _, _) in entries.items() if kind == b"z")
    kind, compressed, pyz = entries[pyz_name]
    toc = read_pyz(pyz)
    replacements, hashes = {}, {}
    modules = project_modules()
    removed = sorted(name for name in toc if name.startswith("hdusign.") and name not in modules)
    for name, file in modules.items():
        filename = str(file.relative_to(ROOT))
        if name in toc:
            _, pos, length = toc[name]
            filename = marshal.loads(zlib.decompress(pyz[pos:pos + length])).co_filename
        data = file.read_bytes()
        replacements[name] = compile(data, filename, "exec", dont_inherit=True, optimize=0)
        hashes[name] = hashlib.sha256(data).hexdigest()
    entries[pyz_name] = kind, compressed, replace_pyz(pyz, dict(replacements), removals=set(removed))
    # The frozen EXE also embeds __main__.py as its executable entry script;
    # updating only the importable module would leave new worker flags unknown.
    entry_kind, entry_compressed, entry_data = entries["__main__"]
    entry_filename = marshal.loads(entry_data).co_filename
    entry_code = compile((ROOT / "hdusign/__main__.py").read_bytes(), entry_filename,
                         "exec", dont_inherit=True, optimize=0)
    entries["__main__"] = entry_kind, entry_compressed, marshal.dumps(entry_code)
    # Versioned resources keep older EXEs and their original interface intact.
    shutil.copytree(ROOT / "hdusign/qml", qml_directory)
    write_exe(output, update_version(bootloader), version, library, entries)
    _, _, _, checked = read_exe(output)
    rebuilt = checked[pyz_name][2]
    if marshal.loads(checked["__main__"][2]) != entry_code:
        raise RuntimeError("Frozen entry script verification failed")
    rebuilt_toc = read_pyz(rebuilt)
    if any(name in rebuilt_toc for name in removed):
        raise RuntimeError("Deleted project modules remain in the frozen archive")
    for name, expected in replacements.items():
        _, pos, size = rebuilt_toc[name]
        actual = marshal.loads(zlib.decompress(rebuilt[pos:pos + size]))
        if actual != expected:
            raise RuntimeError("Compiled module verification failed: " + name)
    if hashlib.sha256(original.read_bytes()).hexdigest() != original_hash:
        raise RuntimeError("The original executable changed during the build")
    report = {"version": __version__, "base_tag": "v2.0", "base_exe_sha256": original_hash,
              "output": output.name, "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
              "project_source_sha256": hashes,
              "removed_project_modules": removed,
              "qml_sha256": {p.relative_to(qml_directory).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                             for p in sorted(qml_directory.rglob("*")) if p.is_file()},
              "build_method": "incremental PyInstaller archive; original native dependencies unchanged"}
    (ROOT / "build").mkdir(exist_ok=True)
    (ROOT / "build/continuous-manifest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (ROOT / f"build/release-{__version__}-manifest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
