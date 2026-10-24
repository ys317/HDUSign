"""Offline regressions: simulated browser results and synthetic audio recorders.

No school login, sign-in request, webhook, or audio device is used.
"""
from __future__ import annotations

from contextlib import ExitStack, redirect_stdout
from io import BytesIO, StringIO, TextIOWrapper
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import subprocess
import sys
import tempfile
import threading
import unittest

import numpy as np

from hdusign import browser
from hdusign import monitor
from hdusign.__main__ import main as app_main
from hdusign.audio import LoopbackSource

ROOT = Path(__file__).resolve().parents[1]


class EntryAndProfileTests(unittest.TestCase):
    def test_profile_cleanup_matches_only_the_exact_user_data_dir(self):
        profile = Path("D:/EchoSign/browser_profile/accounts/account-a")
        self.assertTrue(browser._commandline_uses_profile(
            f'chrome.exe --user-data-dir="{profile}" --type=browser', profile))
        self.assertFalse(browser._commandline_uses_profile(
            f'chrome.exe --user-data-dir="{profile.parent}" --type=browser', profile))
        self.assertFalse(browser._commandline_uses_profile(str(profile), profile))






    def test_source_entrypoint_routes_monitor_commands(self):
        with patch("os.chdir"), patch.object(monitor, "main", return_value=0) as command:
            self.assertEqual(app_main(["test", "class.wav", "--realtime"]), 0)
        command.assert_called_once_with(["test", "class.wav", "--realtime"])

    def test_frozen_redirected_output_handles_non_gbk_transcription(self):
        stdout_data, stderr_data = BytesIO(), BytesIO()
        with TextIOWrapper(stdout_data, encoding="gbk", newline="\n") as stdout, \
                TextIOWrapper(stderr_data, encoding="gbk", newline="\n") as stderr:
            def command(_):
                print("转写含特殊字符：�")
                print("诊断：�", file=sys.stderr)
                return 0
            with patch.object(sys, "frozen", True, create=True), \
                    patch.object(sys, "stdout", stdout), patch.object(sys, "stderr", stderr), \
                    patch("os.chdir"), patch.object(monitor, "main", side_effect=command):
                self.assertEqual(app_main(["code"]), 0)
            stdout.flush()
            stderr.flush()
            self.assertEqual(stdout_data.getvalue().decode("utf-8"), "转写含特殊字符：�\n")
            self.assertEqual(stderr_data.getvalue().decode("utf-8"), "诊断：�\n")




class FakeRecorder:
    def __init__(self, delay=0.002, fail=False):
        self.delay = delay
        self.fail = fail
        self.entered = threading.Event()
        self.closed = threading.Event()
        self.abort = threading.Event()
        self.filled = threading.Event()
        self.reads = 0
        self.thread = None

    def __enter__(self):
        self.thread = threading.current_thread()
        self.entered.set()
        return self

    def __exit__(self, *_):
        self.closed.set()

    def record(self, numframes):
        if self.abort.wait(self.delay) or self.fail:
            raise RuntimeError("simulated device disconnected")
        self.reads += 1
        if self.reads >= 80:
            self.filled.set()
        return np.zeros((numframes, 1), dtype=np.float32)


class CaptureTests(unittest.TestCase):
    def source(self, **kwargs):
        recorder = FakeRecorder(**kwargs)
        source = LoopbackSource.__new__(LoopbackSource)
        source._mic = SimpleNamespace(recorder=lambda **_: recorder)
        source.chunk_frames = 480
        source.speaker_name = "synthetic test device"
        self.addCleanup(self.cleanup, recorder)
        return source, recorder

    @staticmethod
    def cleanup(recorder):
        recorder.abort.set()
        if recorder.thread:
            recorder.thread.join(timeout=2)

    def assert_released(self, recorder):
        self.assertTrue(recorder.closed.is_set())
        self.assertFalse(recorder.thread.is_alive())

    def test_closing_consumer_releases_recorder_and_thread(self):
        source, recorder = self.source()
        chunks = source.chunks()
        next(chunks)
        chunks.close()
        self.assert_released(recorder)

    def test_stop_signal_finishes_with_an_empty_queue(self):
        source, recorder = self.source(delay=0.1)
        stop = threading.Event()
        errors = []

        def consume():
            try:
                list(source.chunks(stop=stop))
            except Exception as exc:
                errors.append(exc)

        consumer = threading.Thread(target=consume, daemon=True)
        consumer.start()
        self.assertTrue(recorder.entered.wait(1))
        stop.set()
        consumer.join(timeout=2)
        self.assertFalse(consumer.is_alive())
        self.assertEqual(errors, [])
        self.assert_released(recorder)

    def test_device_failure_reaches_consumer_and_releases_resources(self):
        source, recorder = self.source(fail=True)
        with self.assertRaisesRegex(RuntimeError, "音频采集失败") as error:
            next(source.chunks())
        self.assertIsInstance(error.exception.__cause__, RuntimeError)
        self.assert_released(recorder)

    def test_repeated_start_stop_does_not_accumulate_threads(self):
        for _ in range(8):
            source, recorder = self.source()
            stop = threading.Event()
            chunks = source.chunks(stop=stop)
            next(chunks)
            stop.set()
            with self.assertRaises(StopIteration):
                next(chunks)
            self.assert_released(recorder)

    def test_full_queue_does_not_prevent_shutdown(self):
        source, recorder = self.source(delay=0.001)
        chunks = source.chunks()
        next(chunks)
        self.assertTrue(recorder.filled.wait(2))
        chunks.close()
        self.assert_released(recorder)

    def test_pre_cancelled_capture_never_opens_device(self):
        source, recorder = self.source()
        stop = threading.Event()
        stop.set()
        self.assertEqual(list(source.chunks(stop=stop)), [])
        self.assertFalse(recorder.entered.is_set())

    def test_pipeline_error_or_interrupt_closes_capture(self):
        for exception in (RuntimeError("ASR failed"), KeyboardInterrupt()):
            with self.subTest(exception=type(exception).__name__), ExitStack() as stack:
                source, recorder = self.source()
                asr = MagicMock()
                asr.accept.side_effect = exception
                stack.enter_context(patch.object(monitor, "LoopbackSource", return_value=source))
                stack.enter_context(patch.object(monitor, "make_engine", return_value=asr))
                stack.enter_context(patch.object(monitor, "build_matchers", return_value=[]))
                stack.enter_context(patch.object(monitor, "make_alerter", return_value=MagicMock()))
                stack.enter_context(patch.object(monitor, "make_watcher", return_value=None))
                stack.enter_context(redirect_stdout(StringIO()))
                if isinstance(exception, KeyboardInterrupt):
                    monitor.cmd_run({})
                else:
                    with self.assertRaisesRegex(RuntimeError, "ASR failed"):
                        monitor.cmd_run({})
                self.assert_released(recorder)

    def test_stop_during_decode_discards_final_and_flush_notifications(self):
        stop = threading.Event()
        asr = MagicMock()

        def accept(_):
            stop.set()
            return ["签到码1234"], ""

        asr.accept.side_effect = accept
        asr.flush.return_value = ["签到码1234"]
        watcher = MagicMock()
        with redirect_stdout(StringIO()):
            monitor.run_pipeline([np.zeros(160)], asr, [], MagicMock(), watcher, stop=stop)
        watcher.feed.assert_not_called()

    def test_normal_end_still_processes_final_transcription(self):
        asr = MagicMock()
        asr.flush.return_value = ["签到码1234"]
        watcher = MagicMock()
        watcher.feed.return_value = [("1234", "签到码1234")]
        with redirect_stdout(StringIO()):
            monitor.run_pipeline([], asr, [], MagicMock(), watcher)
        watcher.on_code.assert_called_once_with("1234", "签到码1234")

    def test_windowed_replay_without_stdout_still_dispatches_the_final_code(self):
        asr, watcher = MagicMock(), MagicMock()
        asr.accept.return_value = ([], "一二三")
        asr.flush.return_value = ["一二三四"]
        watcher.feed_partial.return_value = []
        watcher.feed.return_value = [("1234", "一二三四")]
        with patch.object(sys, "stdout", None):
            monitor.run_pipeline([np.zeros(160)], asr, [], MagicMock(), watcher)
        watcher.on_code.assert_called_once_with("1234", "一二三四")

    def test_stop_during_matching_does_not_dispatch_notifications(self):
        stop = threading.Event()
        asr = MagicMock()
        asr.accept.return_value = (["签到码1234"], "")
        asr.flush.return_value = []
        matcher = MagicMock()
        matcher.match.side_effect = lambda _: (stop.set() or ("high", "签到"))
        watcher, alerter = MagicMock(), MagicMock()
        with redirect_stdout(StringIO()):
            monitor.run_pipeline([np.zeros(160)], asr, [matcher], alerter, watcher, stop=stop)
        alerter.notify.assert_not_called()
        watcher.feed.assert_not_called()

    def test_stopped_watcher_does_not_notify(self):
        stop = threading.Event()
        alerter = MagicMock()
        watcher = monitor.make_watcher({}, alerter, stop)
        stop.set()
        watcher.on_code("1234", "签到码1234")
        alerter.notify.assert_not_called()


    def test_wav_replay_closes_source_on_completion_and_failure(self):
        for error in (None, RuntimeError("decode failed")):
            with self.subTest(error=error), ExitStack() as stack:
                source = MagicMock()
                stack.enter_context(patch.object(monitor, "WavFileSource", return_value=source))
                stack.enter_context(patch.object(monitor, "make_alerter"))
                stack.enter_context(patch.object(monitor, "build_matchers", return_value=[]))
                stack.enter_context(patch.object(monitor, "make_engine"))
                stack.enter_context(patch.object(monitor, "run_pipeline", side_effect=error))
                if error:
                    with self.assertRaisesRegex(RuntimeError, "decode failed"):
                        monitor.cmd_test({}, "fake.wav", False)
                else:
                    monitor.cmd_test({}, "fake.wav", False)
                source.chunks.return_value.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
