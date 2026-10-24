"""Archive replacement includes every source and drops removed app modules."""
import importlib.util
import marshal
from pathlib import Path
import struct
import unittest
import zlib

from tools import build_continuous as build
from tools.frozen_archive import read_pyz, replace_pyz


class IncrementalBuildTests(unittest.TestCase):
    def archive(self):
        payload = bytearray(b"PYZ\0" + importlib.util.MAGIC_NUMBER + b"\0\0\0\0")
        toc = []
        for name in ('hdusign', 'hdusign.attendance', 'third_party'):
            data = zlib.compress(marshal.dumps(compile('original = True', name, 'exec')))
            toc.append((name, (int(name == 'hdusign'), len(payload), len(data))))
            payload.extend(data)
        struct.pack_into('!i', payload, 8, len(payload))
        payload.extend(marshal.dumps(toc))
        return bytes(payload)

    def test_deleted_module_is_removed_and_dependencies_are_preserved(self):
        original = self.archive()
        code = compile('updated = True', 'updated', 'exec')
        rebuilt = replace_pyz(original, {'hdusign': code, 'hdusign.rules': code}, removals={'hdusign.attendance'})
        toc = read_pyz(rebuilt)
        self.assertNotIn('hdusign.attendance', toc)
        for name in ('hdusign', 'hdusign.rules'):
            _, pos, size = toc[name]
            self.assertEqual(marshal.loads(zlib.decompress(rebuilt[pos:pos + size])), code)
        _, pos, size = read_pyz(original)['third_party']
        _, new_pos, new_size = toc['third_party']
        self.assertEqual(original[pos:pos + size], rebuilt[new_pos:new_pos + new_size])

    def test_current_sources_include_recognition_and_legacy_interface_changes(self):
        modules = build.project_modules()
        self.assertIn('hdusign.rules', modules)
        self.assertIn('hdusign.gui', modules)
        self.assertNotIn('hdusign.attendance', modules)
        self.assertTrue(all(path.is_file() for path in modules.values()))

    def test_release_name_and_windows_resource_follow_version_and_content(self):
        self.assertEqual(build.EXE_NAME, f'HDUSign-v{build.__version__}-{build.UPDATE_CONTENT}.exe')
        self.assertIn((build.EXE_NAME + '\0').encode('utf-16le'), build.version_resource())
