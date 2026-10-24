"""Synthetic audio reaches WeChat payloads; no browser/signing is involved."""
from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import numpy as np

from hdusign import browser, monitor
from hdusign.alert import Alerter
from hdusign.__main__ import main


class NotificationOnlyTests(unittest.TestCase):
    def test_replay_with_legacy_auto_sign_enabled_only_sends_code_notification(self):
        cfg = {'auto_sign': {'enabled': True, 'prewarm_browser': True},
               'code_watch': {'log_file': os.devnull}, 'rules': {'strong': ['签到']}}
        asr = Mock()
        asr.accept.return_value = (['签到码零一二三'], '')
        asr.flush.return_value = []
        source = Mock()
        source.chunks.return_value = (frame for frame in [np.zeros(4000, dtype=np.float32)])
        completed = threading.Event()
        payloads = []

        def fake_http(request, **kwargs):
            payloads.append(json.loads(request.data))
            completed.set()
            response = Mock()
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            response.read.return_value = b'{"errcode":0}'
            return response

        with tempfile.TemporaryDirectory() as directory:
            alerter = Alerter(str(Path(directory) / 'alerts.jsonl'),
                              webhook_url='https://example.invalid/robot', webhook_levels=['code'],
                              student_name='测试同学', course_name='测试课')
            with patch.object(monitor, 'make_engine', return_value=asr), \
                    patch.object(monitor, 'WavFileSource', return_value=source), \
                    patch.object(monitor, 'make_alerter', return_value=alerter), \
                    patch('urllib.request.urlopen', side_effect=fake_http), \
                    patch('playwright.sync_api.sync_playwright', side_effect=AssertionError('Unexpected browser')), \
                    patch('subprocess.Popen', side_effect=AssertionError('Unexpected child')), \
                    redirect_stdout(StringIO()):
                monitor.cmd_test(cfg, 'synthetic.wav', False)
                self.assertTrue(completed.wait(2))
            self.assertEqual(len(payloads), 1)
            body = payloads[0]['markdown']['content']
            for expected in ('签到码: 0123', '测试同学', '测试课'):
                self.assertIn(expected, body)

    def test_removed_cli_commands_are_rejected_without_opening_browser(self):
        for args in (['--sign', '0123'], ['--sign-worker', '--exchange-dir', 'unused'], ['--login']):
            with self.subTest(args=args), patch('os.chdir'), redirect_stdout(StringIO()), \
                    patch('sys.stderr', new_callable=StringIO), \
                    patch('playwright.sync_api.sync_playwright', side_effect=AssertionError('Unexpected browser')):
                with self.assertRaises(SystemExit) as error:
                    main(args)
                self.assertEqual(error.exception.code, 2)

    def test_signing_functions_are_removed(self):
        for name in ('sign', 'sign_worker', 'sign_with_code', 'open_sign_in', 'login'):
            self.assertFalse(hasattr(browser, name))

    def test_saving_settings_cleans_obsolete_signing_keys(self):
        from hdusign.demo import DemoController, prepare_demo
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepare_demo(root)
            controller = DemoController(root)
            controller.shutdown()
            try:
                controller.cfg.update(auto_sign={'enabled': True}, location={'lat': 'invalid'})
                self.assertTrue(controller.save())
                for name in ('auto_sign', 'location'):
                    self.assertNotIn(name, controller.cfg)
                for name in ('autoSign', 'latitude', 'longitude'):
                    self.assertNotIn(name, controller._form)
            finally:
                controller.shutdown()
