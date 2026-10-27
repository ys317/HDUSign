"""Restored signing stays scoped to a complete class and one account; no live I/O."""
from contextlib import ExitStack
from dataclasses import asdict
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from hdusign import attendance, monitor
from hdusign.accounts import session_credentials
from hdusign.lessons import DeadlineStop, group_lessons
import test_live_monitor as fixtures
from test_lessons import BASE, lessons


class ContinuousSigningTests(unittest.TestCase):
    setUp = fixtures.LiveMonitorTests.setUp
    successful_frames = fixtures.LiveMonitorTests.successful_frames
    setup_pipeline = fixtures.LiveMonitorTests.setup_pipeline

    def test_one_prepared_signer_survives_third_stream_switch_and_stops_at_class_end(self):
        self.cfg['continuous_course'] = asdict(group_lessons(lessons())[0])
        self.cfg['code_watch'] = {'log_file': os.devnull}
        make_watcher = monitor.make_watcher
        with ExitStack() as stack:
            stack.enter_context(patch.object(monitor.time, 'time', lambda: BASE + self.clock.now))
            signer_stops = []

            def frames(stream_stop):
                signer_stop = f.make_signer.call_args.args[2]
                signer_stops.append(signer_stop)
                self.assertFalse(signer_stop.is_set())
                f.signer.prepare.assert_called_once()
                yield self.frame
                self.clock.advance(stream_stop.deadline - BASE - self.clock.now)
                if len(signer_stops) == 1:
                    self.assertFalse(signer_stop.is_set(), 'third-cell switch must not cancel signing')

            f = self.setup_pipeline(stack, frames)
            f.asr.accept.side_effect = [(['签到码1234'], ''), (['签到码5678'], '')]
            stack.enter_context(patch.object(monitor, 'make_watcher', side_effect=make_watcher))
            monitor.cmd_run_live(self.cfg, self.stop)
        f.make_signer.assert_called_once()
        f.signer.close.assert_called_once()
        self.assertEqual([call.args[0] for call in f.signer.submit.call_args_list], ['1234', '5678'])
        self.assertIs(signer_stops[0], signer_stops[1])
        self.assertTrue(signer_stops[0].is_set())
        self.assertEqual([call.args[0] for call in f.client.select_lesson.call_args_list], ['123', '125'])

    def test_expired_course_does_not_prewarm_or_start_signing(self):
        self.cfg['continuous_course'] = asdict(group_lessons(lessons())[0])
        self.clock.advance(8820)
        with ExitStack() as stack:
            stack.enter_context(patch.object(monitor.time, 'time', lambda: BASE + self.clock.now))
            f = self.setup_pipeline(stack)
            monitor.cmd_run_live(self.cfg, self.stop)
        f.make_signer.assert_not_called()
        f.source.assert_not_called()

    def test_class_deadline_blocks_code_dispatch_and_preparation(self):
        signer, alerter = Mock(), Mock()
        stop = DeadlineStop(self.stop, 10, self.clock)
        watcher = monitor.make_watcher({}, alerter, signer, stop)
        self.clock.advance(10)
        watcher.on_prepare()
        watcher.on_code('1234', '签到码1234')
        signer.prepare.assert_not_called()
        signer.submit.assert_not_called()
        alerter.notify.assert_not_called()


@unittest.skipUnless(sys.platform == 'win32', 'DPAPI account environment')
class SigningAccountTests(unittest.TestCase):
    def test_nested_source_child_loads_only_its_encrypted_account_credentials(self):
        source = Path(__file__).resolve().parents[1]
        script = r'''
import json, os, sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
from hdusign import attendance, browser
from hdusign.__main__ import main
root = Path(os.environ['HDUSIGN_ACCOUNT_ROOT']).resolve()
credentials = browser.load_secrets()
def fake_sign(args):
    assert args == ['0123']
    assert Path.cwd() == root
    assert browser.ROOT == root and attendance.ROOT == root
    assert credentials == {'skl_username': 'fixture-account', 'skl_password': 'fixture-password'}
    return 0
with patch.object(browser, 'sign', side_effect=fake_sign):
    assert main(['--sign', '0123']) == 0
print('account-isolated')
'''
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, HDUSIGN_ACCOUNT_ROOT=directory)
            session_credentials(directory, {'skl_username': 'fixture-account',
                                            'skl_password': 'fixture-password'}, environment=env)
            result = subprocess.run([sys.executable, '-X', 'utf8', '-c', script, str(source)],
                                    cwd=directory, env=env, capture_output=True, text=True,
                                    encoding='utf-8', timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'account-isolated')
            with patch.dict(os.environ, env), patch.object(sys, 'frozen', False, create=True):
                command = attendance.browser_command('--version')
            self.assertIn(str(source / 'tools' / 'account_worker_bootstrap.py'), command)
            self.assertNotIn('fixture-password', command)
            result = subprocess.run(command, cwd=directory, env=env, capture_output=True,
                                    text=True, encoding='utf-8', timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(result.stdout.startswith('HDUSign v'))
            self.assertEqual(list(Path(directory).iterdir()), [])
