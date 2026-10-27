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
    def test_replay_with_auto_sign_disabled_only_sends_code_notification(self):
        cfg = {'auto_sign': {'enabled': False, 'prewarm_browser': True},
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

    def test_signing_cli_dispatch_preserves_arguments_without_starting_real_browser(self):
        for args, target, forwarded in (
                (['--sign', '0123', '--captcha-boost=off'], 'sign', ['0123', '--captcha-boost=off']),
                (['--sign-worker', '--exchange-dir', 'unused'], 'sign_worker', ['--exchange-dir', 'unused']),
                (['--login'], 'login', None)):
            with self.subTest(args=args), patch('os.chdir'), redirect_stdout(StringIO()), \
                    patch.object(browser, target, return_value=0) as dispatch:
                self.assertEqual(main(args), 0)
                if forwarded is None:
                    dispatch.assert_called_once_with()
                else:
                    dispatch.assert_called_once_with(forwarded)

    def test_demo_explicitly_disables_signing_even_when_template_enables_it(self):
        from hdusign.demo import prepare_demo
        import yaml
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepare_demo(root)
            cfg = yaml.safe_load((root / 'config.yaml').read_text('utf-8'))
            self.assertIs(cfg['auto_sign']['enabled'], False)

    def test_saving_settings_preserves_signing_location_and_local_captcha_options(self):
        from hdusign.demo import DemoController, prepare_demo
        import yaml
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepare_demo(root)
            controller = DemoController(root)
            try:
                controller.cfg['auto_sign'].update(prewarm_browser=False, captcha_trajectory=False,
                                                    timeout_seconds=240, local_option='keep')
                controller._form.update(autoSign=True, latitude='30.5', longitude='120.5')
                self.assertTrue(controller.save())
                cfg = yaml.safe_load((root / 'config.yaml').read_text('utf-8'))
                self.assertEqual(cfg['auto_sign'], controller.cfg['auto_sign'])
                self.assertTrue(cfg['auto_sign']['enabled'])
                self.assertFalse(cfg['auto_sign']['prewarm_browser'])
                self.assertFalse(cfg['auto_sign']['captcha_trajectory'])
                self.assertEqual(cfg['auto_sign']['timeout_seconds'], 240)
                self.assertEqual(cfg['auto_sign']['local_option'], 'keep')
                self.assertEqual(cfg['location'], {'lat': 30.5, 'lng': 120.5})
                for key in ('autoSign', 'latitude', 'longitude'):
                    self.assertIn(key, controller._form)
            finally:
                controller.shutdown()
