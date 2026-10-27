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
from hdusign.attendance import AutoSigner
from hdusign.audio import LoopbackSource
from hdusign.attendance import SignResult, classify_response

ROOT = Path(__file__).resolve().parents[1]


class SignResultTests(unittest.TestCase):
    def test_profile_cleanup_matches_only_the_exact_user_data_dir(self):
        profile = Path("D:/EchoSign/browser_profile/accounts/account-a")
        self.assertTrue(browser._commandline_uses_profile(
            f'chrome.exe --user-data-dir="{profile}" --type=browser', profile))
        self.assertFalse(browser._commandline_uses_profile(
            f'chrome.exe --user-data-dir="{profile.parent}" --type=browser', profile))
        self.assertFalse(browser._commandline_uses_profile(str(profile), profile))

    def test_business_failure_containing_success_word_stays_failure(self):
        result = classify_response("1234", 200, {"code": 400, "message": "签到未成功，请重试"})
        self.assertEqual(result.status, "failure")

    def test_only_explicit_business_success_qualifies(self):
        cases = [
            (200, {"code": 200, "msg": "签到成功"}, "success"),
            (200, {"code": "200"}, "success"),
            (200, {"code": 200, "success": False}, "failure"),
            (503, {"code": 200}, "failure"),
            (200, {"code": 2000}, "failure"),
            (200, {"code": True}, "unknown"),
            (200, {"message": "成功"}, "unknown"),
            (200, None, "unknown"),
            (200, "签到成功", "unknown"),
        ]
        for http_status, payload, expected in cases:
            with self.subTest(http_status=http_status, payload=payload):
                self.assertEqual(classify_response("1234", http_status, payload).status, expected)

    def test_response_must_match_endpoint_method_and_current_code(self):
        endpoint = "https://skl.hdu.edu.cn/api/ali-nvc/captcha-verify"
        cases = [
            (endpoint + "?code=1234", "POST", True),
            (endpoint + "?captchaVerifyParam=token&userid=42&code=1234&latitude=30.3&longitude=120.3&t=1",
             "POST", True),
            (endpoint + "?code=4321", "POST", False),
            (endpoint + "?code=1234&code=4321", "POST", False),
            (endpoint, "POST", False),
            (endpoint + "?code=1234", "GET", False),
            ("https://skl.hdu.edu.cn/api/login?code=1234", "POST", False),
            ("https://example.com/api/ali-nvc/captcha-verify?code=1234", "POST", False),
        ]
        for url, method, expected in cases:
            response = SimpleNamespace(url=url, request=SimpleNamespace(method=method))
            with self.subTest(url=url, method=method):
                self.assertEqual(browser.is_sign_response(response, "1234"), expected)

    def test_browser_writes_structured_result_without_stdout(self):
        for status in ("success", "failure", "unknown"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
                output = Path(folder) / "result.json"
                progress = Path(folder) / "progress.json"
                result = SignResult(status, "1234", "演示结果")
                context = MagicMock()
                context.pages = [MagicMock()]
                runtime = MagicMock()
                runtime.__enter__.return_value.chromium.launch_persistent_context.return_value = context
                stack.enter_context(patch.object(browser, "sync_playwright", return_value=runtime))
                stack.enter_context(patch.object(browser, "load_secrets", return_value={}))
                stack.enter_context(patch.object(browser, "_cleanup_stale_profile"))
                stack.enter_context(patch.object(browser, "_location", return_value=(0, 0)))
                stack.enter_context(patch.object(browser, "ensure_logged_in"))
                def sign_with_progress(*args, on_stage):
                    on_stage("verification_required", 1)
                    return result
                stack.enter_context(patch.object(browser, "sign_with_code", side_effect=sign_with_progress))
                stack.enter_context(patch.object(sys, "stdout", None))
                exit_code = browser.sign(["1234", "--result-file", str(output), "--progress-file", str(progress)])
                self.assertEqual(exit_code, result.exit_code)
                self.assertEqual(SignResult.read(output, "1234"), result)
                self.assertIn('"verification_required"', progress.read_text(encoding="utf-8"))
                context.close.assert_called_once()

    def test_frozen_entrypoint_preserves_result_file_argument(self):
        captured = []
        args = ["HDUSign.exe", "--sign", "1234", "--result-file", "result.json"]

        def fake_main(argv):
            captured.extend(argv)
            return 1

        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "argv", args), \
                patch("os.chdir"), patch.object(browser, "sign", side_effect=fake_main):
            exit_code = app_main()
        self.assertEqual(exit_code, 1)
        self.assertEqual(captured, ["1234", "--result-file", "result.json"])

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


class DispatcherTests(unittest.TestCase):
    def dispatch(self, result=None, stdout="", returncode=0, malformed=False, frozen=False):
        notifications = []
        commands = []
        paths = []

        def child(command, **_):
            commands.append(command)
            path = Path(command[command.index("--result-file") + 1])
            paths.append(path)
            if malformed:
                path.write_text("{invalid", encoding="utf-8")
            elif result is not None:
                result.write(path)
            return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

        signer = AutoSigner(lambda reason, text: notifications.append((reason, text)))
        with patch.object(signer, "_run_child", side_effect=child), \
                patch.object(sys, "frozen", frozen, create=True), redirect_stdout(StringIO()):
            signer._sign_one("1234")
        self.assertEqual(len(notifications), 1)
        self.assertTrue(all(not path.exists() for path in paths), "Temporary result files must be cleaned up")
        return notifications[0], commands[0]

    def test_log_text_cannot_report_success(self):
        for text in ("签到未成功", '[结果] {"code":200}', "登录成功，签到失败"):
            with self.subTest(text=text):
                notification, _ = self.dispatch(stdout=text)
                self.assertIn("结果未知", notification[0])

    def test_explicit_failure_is_reported_even_when_message_contains_success(self):
        result = SignResult("failure", "1234", "签到未成功")
        notification, _ = self.dispatch(result=result, stdout="登录成功", returncode=1)
        self.assertIn("自动签到失败", notification[0])

    def test_success_works_for_source_and_windowed_exe_without_logs(self):
        for frozen in (False, True):
            with self.subTest(frozen=frozen):
                notification, command = self.dispatch(SignResult("success", "1234", "OK"), stdout=None, frozen=frozen)
                self.assertIn("自动签到成功", notification[0])
                self.assertIn("--sign", command)
                if frozen:
                    self.assertEqual(command[:2], [sys.executable, "--sign"])
                else:
                    self.assertEqual(command[:5], [sys.executable, "-X", "utf8", "-m", "hdusign"])

    def test_abnormal_exit_never_reports_success(self):
        notification, _ = self.dispatch(SignResult("success", "1234", "OK"), returncode=1)
        self.assertIn("结果未知", notification[0])

    def test_mismatched_or_malformed_result_is_unknown(self):
        for result, malformed in ((SignResult("success", "4321", "OK"), False), (None, True)):
            with self.subTest(result=result, malformed=malformed):
                notification, _ = self.dispatch(result=result, malformed=malformed)
                self.assertIn("结果未知", notification[0])

    def test_timeout_is_unknown_and_does_not_retry(self):
        notifications = []
        signer = AutoSigner(lambda reason, text: notifications.append((reason, text)))
        with patch.object(signer, "_run_child", side_effect=subprocess.TimeoutExpired("test", 1)) as run, \
                redirect_stdout(StringIO()):
            signer._sign_one("1234")
        run.assert_called_once()
        self.assertIn("结果未知", notifications[0][0])


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

    def test_stopped_watcher_does_not_queue_a_browser_task(self):
        stop = threading.Event()
        alerter, signer = MagicMock(), MagicMock()
        watcher = monitor.make_watcher({}, alerter, signer, stop)
        stop.set()
        watcher.on_code("1234", "签到码1234")
        alerter.notify.assert_not_called()
        signer.submit.assert_not_called()

    def test_monitor_closes_signer_even_if_model_initialization_fails(self):
        signer = MagicMock()
        with ExitStack() as stack:
            stack.enter_context(patch.object(monitor, "LoopbackSource"))
            stack.enter_context(patch.object(monitor, "make_alerter"))
            stack.enter_context(patch.object(monitor, "make_auto_signer", return_value=signer))
            stack.enter_context(patch.object(monitor, "build_matchers", return_value=[]))
            stack.enter_context(patch.object(monitor, "make_engine", side_effect=RuntimeError("model error")))
            stack.enter_context(redirect_stdout(StringIO()))
            with self.assertRaisesRegex(RuntimeError, "model error"):
                monitor.cmd_run({})
        signer.close.assert_called_once_with()

    def test_wav_replay_drains_on_completion_and_cancels_on_failure(self):
        for error in (None, RuntimeError("decode failed")):
            with self.subTest(error=error), ExitStack() as stack:
                signer = MagicMock()
                source = MagicMock()
                stack.enter_context(patch.object(monitor, "WavFileSource", return_value=source))
                stack.enter_context(patch.object(monitor, "make_alerter"))
                stack.enter_context(patch.object(monitor, "make_auto_signer", return_value=signer))
                stack.enter_context(patch.object(monitor, "build_matchers", return_value=[]))
                stack.enter_context(patch.object(monitor, "make_engine"))
                stack.enter_context(patch.object(monitor, "run_pipeline", side_effect=error))
                if error:
                    with self.assertRaisesRegex(RuntimeError, "decode failed"):
                        monitor.cmd_test({}, "fake.wav", False)
                else:
                    monitor.cmd_test({}, "fake.wav", False)
                signer.close.assert_called_once_with(cancel=error is not None)
                source.chunks.return_value.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
