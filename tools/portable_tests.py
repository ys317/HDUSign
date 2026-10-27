"""Run source tests with the exact dependencies already shipped in HDUSign.

Invoke with Python 3.13.7: python tools/portable_tests.py test_live ...
Only project source is imported from this checkout; third-party modules are
loaded from the original, verified portable archive and its native DLLs.
"""
from __future__ import annotations

import importlib.abc
import importlib.machinery
import importlib.util
import marshal
import os
from pathlib import Path
import sys
import unittest
import zlib

SOURCE = Path(__file__).resolve().parents[1]
PORTABLE = SOURCE.parent
os.environ["HDUSIGN_PORTABLE_DEPENDENCIES"] = "1"
sys.path[:0] = [str(SOURCE), str(SOURCE / "tests"), str(PORTABLE / "_internal")]
from tools.frozen_archive import read_exe, read_pyz

_stub, _version, _library, entries = read_exe(PORTABLE / "HDUSign.exe")
pyz = next(data for kind, _, data in entries.values() if kind == b"z")
toc = read_pyz(pyz)
handles = [os.add_dll_directory(str(path)) for path in
           (PORTABLE / "_internal", PORTABLE / "_internal/PySide6", PORTABLE / "_internal/shiboken6")]


class PackagedLoader(importlib.abc.Loader):
    def __init__(self, name):
        self.name = name
        self.package = toc[name][0] == 1
        relative = Path(*name.split("."))
        self.path = str(PORTABLE / "_internal" / (relative / "__init__.pyc" if self.package
                                                  else relative.with_suffix(".pyc")))

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        module.__file__ = self.path
        if self.package:
            module.__path__ = [str(Path(self.path).parent)]
        exec(self.get_code(self.name), module.__dict__)

    def get_code(self, name):
        kind, pos, size = toc[name]
        return marshal.loads(zlib.decompress(pyz[pos:pos + size]))

    def get_filename(self, name):
        return self.path

    def is_package(self, name):
        return self.package

    def get_resource_reader(self, name):
        from importlib.resources.readers import FileReader
        return FileReader(self)


class PackagedFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "hdusign" or name.startswith("hdusign.") or name not in toc:
            return None
        loader = PackagedLoader(name)
        return importlib.util.spec_from_loader(name, loader, origin=loader.path,
                                              is_package=loader.package)


sys.meta_path.insert(0, PackagedFinder())
test_qt = SOURCE / "build/test-libs/PySide6"
if test_qt.is_dir():
    handles.append(os.add_dll_directory(str(test_qt)))
    import PySide6
    PySide6.__path__.append(str(test_qt))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if __name__ == "__main__":
    unittest.main(module=None, argv=[sys.argv[0], *sys.argv[1:]], verbosity=2)
