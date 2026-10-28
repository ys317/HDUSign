"""Global glass preferences use isolated files and a fake native compositor."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from PySide6.QtGui import QGuiApplication
from hdusign.appearance import Appearance, DEFAULTS

APP = QGuiApplication.instance() or QGuiApplication([])
APP.setQuitOnLastWindowClosed(False)


class AppearanceTests(unittest.TestCase):
    def test_preview_save_restart_and_reset(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            backend = Mock()
            backend.apply.return_value = True
            appearance = Appearance(root, backend=backend)
            appearance.attach(Mock())
            self.assertTrue(appearance.active)
            appearance.setValue('transparency', 72)
            appearance.setValue('tint', 18)
            self.assertTrue(appearance.dirty)
            self.assertFalse(appearance.path.exists())
            self.assertTrue(appearance.save())
            restored = Appearance(root, backend=backend)
            self.assertEqual(restored.values.value('transparency'), 72)
            self.assertEqual(restored.values.value('tint'), 18)
            self.assertFalse(restored.dirty)
            restored.reset()
            self.assertEqual(restored.values.value('transparency'), DEFAULTS['transparency'])
            self.assertTrue(restored.dirty)
            appearance.setValue('enabled', False)
            self.assertFalse(appearance.active)
            self.assertFalse(backend.apply.call_args.args[1])

    def test_native_failure_has_opaque_fallback_and_slider_does_not_reapply_dwm(self):
        with tempfile.TemporaryDirectory() as temporary:
            backend = Mock()
            backend.apply.return_value = False
            appearance = Appearance(Path(temporary), backend=backend)
            appearance.attach(Mock())
            self.assertFalse(appearance.active)
            self.assertIn('普通外观', appearance.status)
            backend.reset_mock()
            appearance.setValue('transparency', 90)
            backend.apply.assert_not_called()
            appearance.setDark(False)
            backend.apply.assert_called_once()

    def test_invalid_settings_and_write_failure_do_not_overwrite_saved_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / 'appearance.json'
            path.write_text(json.dumps({'enabled': 'true', 'transparency': 200,
                                        'tint': float('nan'), 'highlight': -4}), 'utf-8')
            appearance = Appearance(root)
            self.assertTrue(appearance.values.value('enabled'))
            self.assertEqual(appearance.values.value('transparency'), 100)
            self.assertEqual(appearance.values.value('tint'), DEFAULTS['tint'])
            self.assertEqual(appearance.values.value('highlight'), 0)
            original = path.read_bytes()
            appearance.setValue('tint', 10)
            with patch('hdusign.appearance.write_json', side_effect=PermissionError()):
                self.assertFalse(appearance.save())
            self.assertEqual(path.read_bytes(), original)
            self.assertTrue(appearance.dirty)
            self.assertIn('保存失败', appearance.feedback)
            path.write_text('{broken json', 'utf-8')
            fallback = Appearance(root)
            self.assertEqual(fallback.values.value('transparency'), DEFAULTS['transparency'])
