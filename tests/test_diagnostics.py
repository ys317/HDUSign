"""Audio-window, timing, durability and account isolation diagnostics tests."""
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import wave

import numpy as np

from hdusign.diagnostics import Diagnostics, Journal, JournalOutput, RATE
from hdusign.account_worker import ProcessServices
from hdusign.accounts import write_json
from hdusign import monitor
import test_desktop_controller as desktop
import test_live_monitor as live_fixtures
from contextlib import ExitStack

BASE = 1792146600.0


class Clock:
    def __init__(self, now=BASE-120):
        self.now = now
    def __call__(self):
        return self.now
    def advance(self, seconds):
        self.now += seconds


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.clock = Clock()
        self.journal = Journal(self.root, self.clock, self.clock)
        self.addCleanup(self.journal.close)
        self.cfg = {'diagnostics': {'enabled': True},
                    'notification_context': {'username': 'fixture-a', 'course_name': '诊断测试'}}

    def diagnostic(self, planned=True):
        plan = SimpleNamespace(upcoming_start=lambda now: BASE) if planned else None
        return Diagnostics(self.cfg, plan, journal=self.journal, wall=self.clock, mono=self.clock)

    def events(self):
        return [json.loads(line) for line in (self.root / 'events.jsonl').read_text('utf-8').splitlines()]

    def test_three_minute_recording_bounds_and_separate_text_timestamps(self):
        diagnostic = self.diagnostic()
        diagnostic.begin_stream('123')
        for i in range(240):
            self.clock.advance(1)
            sample = np.zeros(RATE, dtype=np.float32) if i < 120 else np.full(RATE, .25, dtype=np.float32)
            diagnostic.audio(sample)
            partial = '课堂开始' if i >= 122 else ''
            diagnostic.asr_result(sample, .01, partial, [])
            if i == 136:
                diagnostic.final('课堂开始，下面讲解。')
        diagnostic.close()
        summary = json.loads((self.root / 'summary.json').read_text('utf-8'))
        self.assertEqual(summary['recorded_audio_seconds'], 180)
        with wave.open(str(self.root / 'startup-audio.wav'), 'rb') as recording:
            self.assertEqual((recording.getframerate(), recording.getnchannels(), recording.getsampwidth()), (RATE, 1, 2))
            self.assertEqual(recording.getnframes(), 180 * RATE)
            first = np.frombuffer(recording.readframes(RATE), dtype='<i2')
            self.assertTrue(np.all(first == 0))
            recording.setpos(179 * RATE)
            last = np.frombuffer(recording.readframes(RATE), dtype='<i2')
            self.assertTrue(np.all(last == 8192))
        timing = summary['timings']
        self.assertEqual(timing['first_audio']['unix_time'], BASE-119)
        self.assertEqual(timing['first_partial']['unix_time'], BASE+3)
        self.assertEqual(timing['first_final']['unix_time'], BASE+17)
        metrics = [item for item in self.events() if item['event'] == 'audio_metrics']
        self.assertTrue(any(item['rms_dbfs'] == -120 for item in metrics))
        self.assertTrue(any(-13 < item['rms_dbfs'] < -11 for item in metrics))

    def test_disabled_mode_does_not_create_audio_but_keeps_timings(self):
        self.cfg['diagnostics']['enabled'] = False
        diagnostic = self.diagnostic()
        diagnostic.audio(np.ones(RATE, dtype=np.float32))
        diagnostic.asr_result(np.ones(RATE, dtype=np.float32), .01, '草稿', [])
        diagnostic.final('完整句子')
        diagnostic.close()
        self.assertFalse((self.root / 'startup-audio.wav').exists())
        events = {item['event'] for item in self.events()}
        self.assertTrue({'first_audio', 'first_nonempty_text', 'first_final'} <= events)
        self.assertNotIn('audio_metrics', events)

    def test_recording_starts_at_window_and_midclass_uses_three_minutes(self):
        self.clock.now = BASE-150
        diagnostic = self.diagnostic()
        for _ in range(30):
            self.clock.advance(1)
            diagnostic.audio(np.zeros(RATE, dtype=np.float32))
        self.assertFalse((self.root / 'startup-audio.wav').exists())
        self.clock.advance(1)
        diagnostic.audio(np.zeros(RATE, dtype=np.float32))
        diagnostic.close()
        with wave.open(str(self.root / 'startup-audio.wav'), 'rb') as recording:
            self.assertEqual(recording.getnframes(), RATE)
        self.clock.now = BASE+300
        second = self.diagnostic()
        self.assertEqual(second.window_kind, 'midclass_monitor_start')
        self.assertEqual((second.window_start, second.window_end), (BASE+300, BASE+480))
        second.close()

    def test_gaps_and_stream_switches_have_sample_offsets_without_silence_padding(self):
        diagnostic = self.diagnostic(False)
        source = SimpleNamespace(skipped_seconds=0)
        diagnostic.begin_stream('123')
        self.clock.advance(1)
        diagnostic.audio(np.zeros(RATE, dtype=np.float32), source)
        self.clock.advance(10)
        source.skipped_seconds = 3
        diagnostic.audio(np.zeros(RATE, dtype=np.float32), source)
        diagnostic.begin_stream('125')
        self.clock.advance(1)
        diagnostic.audio(np.zeros(RATE, dtype=np.float32), SimpleNamespace(skipped_seconds=0))
        diagnostic.close()
        summary = json.loads((self.root / 'summary.json').read_text('utf-8'))
        self.assertEqual([row['start_frame'] for row in summary['recording_segments']], [0, RATE, 2*RATE])
        self.assertEqual([row['course_id'] for row in summary['recording_segments']], ['123', '123', '125'])
        self.assertEqual(summary['recorded_audio_seconds'], 3)
        self.assertEqual(summary['metrics']['skipped_audio_seconds'], 3)

    def test_recording_failure_does_not_interrupt_recognition(self):
        diagnostic = self.diagnostic()
        self.clock.advance(1)
        with patch('hdusign.diagnostics.wave.open', side_effect=OSError('fixture disk full')), redirect_stdout(StringIO()):
            diagnostic.audio(np.zeros(RATE, dtype=np.float32))
            diagnostic.asr_result(np.zeros(RATE, dtype=np.float32), .01, '仍在识别', [])
            diagnostic.final('仍在识别')
            diagnostic.close()
        summary = json.loads((self.root / 'summary.json').read_text('utf-8'))
        self.assertTrue(summary['recording_failed'])
        self.assertIn('first_nonempty_text', summary['timings'])

    def test_slow_asr_rtf_is_measured_without_claiming_network_latency(self):
        diagnostic = self.diagnostic(False)
        self.clock.advance(1)
        chunk = np.zeros(RATE, dtype=np.float32)
        diagnostic.audio(chunk)
        diagnostic.asr_result(chunk, 1.5, '', [])
        diagnostic.close()
        summary = json.loads((self.root / 'summary.json').read_text('utf-8'))
        self.assertEqual(summary['metrics']['asr_processing_rtf'], 1.5)
        self.assertEqual(summary['metrics']['max_asr_call_seconds'], 1.5)

    def test_console_keeps_original_timestamp_and_unfinished_partial(self):
        output = StringIO()
        stream = JournalOutput(output, self.journal, 'stdout')
        stream.write('启动')
        self.clock.advance(10)
        stream.write('完成\n\r…识别中: 原始草稿')
        stream.flush()
        stream.finish()
        events = self.events()
        self.assertEqual(events[0]['unix_time'], BASE-120)
        self.assertEqual(events[0]['text'], '启动完成')
        self.assertEqual(events[1]['text'], '…识别中: 原始草稿')
        self.assertIn('启动完成', output.getvalue())

    def test_console_disk_error_does_not_abort_processing(self):
        output = Mock()
        output.write.side_effect = OSError('fixture full disk')
        output.flush.side_effect = OSError('fixture full disk')
        stream = JournalOutput(output, self.journal, 'stdout')
        stream.write('仍在执行\n')
        stream.flush()
        stream.finish()
        self.assertEqual(self.events()[0]['text'], '仍在执行')

    def test_pipeline_records_partial_before_final(self):
        diagnostic = self.diagnostic(False)
        recognizer = Mock()
        recognizer.accept.side_effect = [([], '尚未结束的草稿'), (['完整句子'], '')]
        recognizer.flush.return_value = []
        chunk = np.zeros(RATE, dtype=np.float32)
        def chunks():
            for _ in range(2):
                self.clock.advance(1)
                diagnostic.audio(chunk)
                yield chunk
        with redirect_stdout(StringIO()):
            monitor.run_pipeline(chunks(), recognizer, [], Mock(), show_partial=False, diagnostics=diagnostic)
        diagnostic.close()
        summary = json.loads((self.root / 'summary.json').read_text('utf-8'))
        self.assertLess(summary['timings']['first_partial']['unix_time'], summary['timings']['first_final']['unix_time'])


class PersistentWorkerLogTests(unittest.TestCase):
    def test_five_workers_keep_their_own_audio_and_summaries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            errors, paths = [], {}
            def run(index):
                folder = root / str(index)
                folder.mkdir()
                service = ProcessServices(folder)
                user = 'fixture-' + str(index)
                service.credentials = {'skl_username': user, 'skl_password': 'password-canary-' + str(index)}
                write_json(folder / 'live_session.json', {'username': user})
                cfg = {'diagnostics': {'enabled': True}, 'notification_context': {'username': user}}
                try:
                    service.run('isolation-check', cfg, threading.Event(), check_diagnostics=True,
                                fixture_sample_value=.05 * (index + 1))
                    paths[index] = service.last_log_directory
                except Exception as exc:
                    errors.append(str(exc))
            threads = [threading.Thread(target=run, args=(i,)) for i in range(5)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(25)
            self.assertFalse(errors, errors)
            self.assertEqual(len(paths), 5)
            for index, directory in paths.items():
                summary = json.loads((directory / 'summary.json').read_text('utf-8'))
                self.assertEqual(summary['account'], 'fixture-' + str(index))
                with wave.open(str(directory / 'startup-audio.wav'), 'rb') as recording:
                    value = np.frombuffer(recording.readframes(1), dtype='<i2')[0]
                self.assertEqual(value, round(.05 * (index + 1) * 32768))

    def test_logs_survive_worker_exit_and_capture_only_opted_in_audio(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                service = ProcessServices(root)
                service.credentials = {'skl_username': 'fixture-a', 'skl_password': 'PASSWORD-CANARY'}
                write_json(root / 'live_session.json', {'username': 'fixture-a'})
                cfg = {'diagnostics': {'enabled': enabled},
                       'notification_context': {'username': 'fixture-a', 'course_name': '隔离测试'},
                       'alert': {'webhook': {'url': 'https://example.invalid/WEBHOOK-CANARY'}}}
                result = service.run('isolation-check', cfg, threading.Event(), check_diagnostics=True)
                self.assertTrue(result['diagnostics_checked'])
                directory = service.last_log_directory
                self.assertFalse(service.job_directory.exists())
                self.assertTrue((directory / 'console.log').exists())
                events = (directory / 'events.jsonl').read_text('utf-8')
                self.assertIn('task_started', events)
                self.assertIn('task_finished', events)
                self.assertEqual((directory / 'startup-audio.wav').exists(), enabled)
                for path in directory.iterdir():
                    if path.suffix != '.wav':
                        content = path.read_text('utf-8')
                        self.assertNotIn('PASSWORD-CANARY', content)
                        self.assertNotIn('WEBHOOK-CANARY', content)
                if enabled:
                    with wave.open(str(directory / 'startup-audio.wav'), 'rb') as recording:
                        self.assertEqual(recording.getnframes(), RATE)


class LiveDiagnosticHookTests(unittest.TestCase):
    setUp = live_fixtures.LiveMonitorTests.setUp
    successful_frames = live_fixtures.LiveMonitorTests.successful_frames
    setup_pipeline = live_fixtures.LiveMonitorTests.setup_pipeline

    def test_live_monitor_saves_audio_timings_and_finalizes_files_on_stop(self):
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            directory = Path(temporary)
            f = self.setup_pipeline(stack)
            self.cfg['diagnostics'] = {'enabled': True}
            journal = Journal(directory)
            self.cfg['_run_journal'] = journal
            try:
                monitor.cmd_run_live(self.cfg, self.stop)
            finally:
                journal.close()
            summary = json.loads((directory / 'summary.json').read_text('utf-8'))
            self.assertTrue({'model_loading', 'model_ready', 'first_audio', 'first_nonempty_text'} <= summary['timings'].keys())
            self.assertGreater(summary['recorded_audio_seconds'], 0)
            self.assertTrue((directory / 'startup-audio.wav').exists())


class DiagnosticSettingsTests(unittest.TestCase):
    setUp = desktop.DesktopControllerTests.setUp
    tearDown = desktop.DesktopControllerTests.tearDown

    def test_diagnostic_toggle_is_boolean_and_survives_restart(self):
        self.assertIs(self.c._form['diagnostic'], True)
        self.c.setField('diagnostic', False)
        self.assertIs(self.c._form['diagnostic'], False)
        self.assertTrue(self.c.save())
        saved = json.loads(json.dumps(self.c.cfg))
        self.assertIs(saved['diagnostics']['enabled'], False)
        from hdusign.desktop_controller import Controller
        from hdusign.demo import DemoServices
        other = Controller(self.root, DemoServices(), auto_load=False)
        try:
            self.assertIs(other._form['diagnostic'], False)
        finally:
            other.shutdown()

    def test_upgrade_enables_legacy_account_once_without_changing_other_settings(self):
        import yaml
        from dataclasses import asdict
        from hdusign.desktop_controller import Controller
        from hdusign.demo import DemoServices, demo_courses
        cfg = dict(self.c.cfg)
        cfg['diagnostics'] = {'enabled': False, 'retained_fixture': 'keep'}
        cfg['scheduled_courses'] = [asdict(demo_courses()[1])]
        cfg['hidden_fixture'] = {'key': 'keep-this-too'}
        self.c.config_path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding='utf-8')
        before = yaml.safe_load(self.c.config_path.read_text('utf-8'))
        other = Controller(self.root, DemoServices(), auto_load=False)
        try:
            self.assertTrue(other._form['diagnostic'])
            saved = yaml.safe_load(other.config_path.read_text('utf-8'))
            self.assertTrue(saved['diagnostics']['default_on_applied'])
            self.assertEqual(saved['diagnostics']['retained_fixture'], 'keep')
            self.assertEqual({k: v for k, v in saved.items() if k != 'diagnostics'},
                             {k: v for k, v in before.items() if k != 'diagnostics'})
        finally:
            other.shutdown()

    def test_upgrade_write_failure_keeps_original_configuration_and_reports_error(self):
        import yaml
        from hdusign.desktop_controller import Controller
        from hdusign.demo import DemoServices
        cfg = dict(self.c.cfg, diagnostics={'enabled': False}, retained_fixture='preserve')
        self.c.config_path.write_text(yaml.safe_dump(cfg), encoding='utf-8')
        before = self.c.config_path.read_bytes()
        with patch('hdusign.desktop_controller.write_text_atomic', side_effect=PermissionError()):
            other = Controller(self.root, DemoServices(), auto_load=False)
        try:
            self.assertEqual(other.config_path.read_bytes(), before)
            self.assertEqual(other.cfg['retained_fixture'], 'preserve')
            self.assertFalse(other._form['diagnostic'])
            self.assertTrue(other._state['feedbackError'])
            self.assertIn('保留原设置', other._state['feedback'])
        finally:
            other.shutdown()

    def test_new_account_starts_enabled_when_template_and_existing_account_opt_out(self):
        import yaml
        from hdusign.accounts import AccountStore
        from hdusign.multi_account import AccountManager
        cfg = dict(self.c.cfg, diagnostics={'enabled': False, 'default_on_applied': True})
        self.c.config_path.write_text(yaml.safe_dump(cfg), encoding='utf-8')
        AccountStore(self.root).save('diagnostic-fixture-a', 'fixture-password')
        manager = AccountManager(self.root, auto_load=False)
        try:
            first = manager.controllers['diagnostic-fixture-a']
            self.assertTrue(first._form['diagnostic'])
            first.setField('diagnostic', False)
            self.assertTrue(first.save())
            manager.activate('diagnostic-fixture-b')
            self.assertTrue(manager.active._form['diagnostic'])
            self.assertFalse(first._form['diagnostic'])
            manager2 = AccountManager(self.root, auto_load=False)
            try:
                self.assertFalse(manager2.controllers['diagnostic-fixture-a']._form['diagnostic'])
            finally:
                manager2.shutdown()
        finally:
            manager.shutdown()


if __name__ == '__main__':
    unittest.main()
