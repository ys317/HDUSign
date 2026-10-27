"""Attendance scheduling and cancellation without opening a real browser."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from hdusign.attendance import (
    AutoSigner,
    SignResult,
    _BrowserSession,
    make_auto_signer,
    write_json,
)


class SchedulerTests(unittest.TestCase):
    def test_submit_rejects_non_ascii_or_non_four_digit_codes(self):
        signer = AutoSigner(lambda *_: None)
        self.addCleanup(signer.close)
        for code in ("", "123", "12345", "12a4", "１２３４", None, 1234):
            with self.subTest(code=code):
                self.assertFalse(signer.submit(code))
        self.assertIsNone(signer._pending)
        self.assertIsNone(signer._worker)

    def test_repeated_current_code_does_not_submit_twice_or_replace_new_pending_code(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        signer = AutoSigner(lambda *_: None)
        self.addCleanup(signer.close)
        self.addCleanup(release.set)
        def sign(code):
            calls.append(code)
            if code == "1234":
                entered.set()
                release.wait(3)
        with patch.object(signer, "_sign_one", side_effect=sign):
            signer.submit("1234")
            self.assertTrue(entered.wait(2))
            signer.submit("1234")
            signer.submit("5678")
            detected_at = signer._pending[1]
            signer.submit("5678")
            self.assertEqual(signer._pending, ("5678", detected_at))
            signer.submit("1234")
            release.set()
            signer.close(cancel=False)
        self.assertEqual(calls, ["1234", "5678"])
        self.assertIsNone(signer._active_code)

    def test_submissions_before_worker_start_create_only_one_worker(self):
        gate = threading.Event()
        thread_type = threading.Thread
        signer = AutoSigner(lambda *_: None)
        self.addCleanup(signer.close)
        self.addCleanup(gate.set)

        def delayed_thread(*, target, **kwargs):
            def delayed():
                gate.wait(3)
                target()
            return thread_type(target=delayed, **kwargs)

        with patch("hdusign.attendance.threading.Thread", side_effect=delayed_thread) as factory, \
                patch.object(signer, "_sign_one") as sign:
            self.assertTrue(signer.submit("1234"))
            self.assertTrue(signer.submit("5678"))
            self.assertTrue(signer.submit("9012"))
            self.assertEqual(factory.call_count, 1)
            gate.set()
            signer.close(cancel=False)
        sign.assert_called_once_with("9012")

    def test_concurrent_submissions_are_serial_and_only_latest_waiting_code_runs(self):
        entered, release = threading.Event(), threading.Event()
        signer = AutoSigner(lambda *_: None)
        self.addCleanup(signer.close)
        self.addCleanup(release.set)
        lock = threading.Lock()
        calls = []
        active = peak = 0

        def sign(code):
            nonlocal active, peak
            with lock:
                calls.append(code)
                active += 1
                peak = max(peak, active)
            if code == "1000":
                entered.set()
                release.wait(3)
            with lock:
                active -= 1

        with patch.object(signer, "_sign_one", side_effect=sign):
            signer.submit("1000")
            self.assertTrue(entered.wait(2))
            with ThreadPoolExecutor(max_workers=12) as callers:
                self.assertTrue(all(callers.map(signer.submit, (str(n) for n in range(2000, 2060)))))
            signer.submit("9999")
            self.assertEqual(calls, ["1000"])
            release.set()
            signer.close(cancel=False)
        self.assertEqual(calls, ["1000", "9999"])
        self.assertEqual(peak, 1)
        self.assertFalse(signer.submit("3333"))

    def test_shared_stop_discards_pending_codes_and_rejects_new_work(self):
        stop, entered = threading.Event(), threading.Event()
        signer = AutoSigner(lambda *_: None, stop=stop)
        self.addCleanup(signer.close)
        self.addCleanup(stop.set)
        calls = []

        def sign(code):
            calls.append(code)
            entered.set()
            stop.wait(3)

        with patch.object(signer, "_sign_one", side_effect=sign):
            signer.submit("1234")
            self.assertTrue(entered.wait(2))
            signer.submit("5678")
            stop.set()
            signer.close()
        self.assertEqual(calls, ["1234"])
        self.assertFalse(signer.submit("9012"))

    def test_repeated_sessions_leave_no_attendance_workers(self):
        before = set(threading.enumerate())
        for _ in range(8):
            done = threading.Event()
            signer = AutoSigner(lambda *_: None)
            try:
                with patch.object(signer, "_sign_one", side_effect=lambda _: done.set()):
                    signer.submit("1234")
                    self.assertTrue(done.wait(2))
                    signer.close()
            finally:
                signer.close()
        leftovers = [thread for thread in set(threading.enumerate()) - before
                     if thread.name == "HDUSign attendance"]
        self.assertEqual(leftovers, [])

    def test_task_exception_does_not_strand_later_work(self):
        first = threading.Event()
        signer = AutoSigner(lambda *_: None)
        self.addCleanup(signer.close)
        calls = []

        def sign(code):
            calls.append(code)
            if code == "1234":
                first.set()
                raise RuntimeError("simulated failure")

        with patch.object(signer, "_sign_one", side_effect=sign), redirect_stdout(StringIO()):
            signer.submit("1234")
            self.assertTrue(first.wait(2))
            signer.submit("5678")
            signer.close(cancel=False)
        self.assertEqual(calls, ["1234", "5678"])

    def test_notification_failure_does_not_escape_worker(self):
        def notify(*_):
            raise RuntimeError("notification service unavailable")

        signer = AutoSigner(notify)
        self.addCleanup(signer.close)
        with redirect_stdout(StringIO()) as output:
            signer._notify_verification("1234")
            signer._notify_result(SignResult("success", "1234", "OK"))
        self.assertIn("签到通知失败", output.getvalue())


class ConfigurationTests(unittest.TestCase):
    def test_make_auto_signer_uses_default_for_invalid_or_unbounded_timeout(self):
        for value in (None, "bad", True, float("nan"), float("inf"), 0, 29, 1801):
            with self.subTest(value=value):
                alerter = MagicMock()
                signer = make_auto_signer(
                    {"auto_sign": {"enabled": True, "timeout_seconds": value}}, alerter)
                self.assertIsNotNone(signer)
                self.assertEqual(signer.timeout_s, 180)
                signer.close()

    def test_make_auto_signer_accepts_bounded_timeout(self):
        signer = make_auto_signer(
            {"auto_sign": {"enabled": True, "timeout_seconds": "600"}}, MagicMock())
        self.assertIsNotNone(signer)
        self.addCleanup(signer.close)
        self.assertEqual(signer.timeout_s, 600)


class PreparedSubmitCleanupTests(unittest.TestCase):
    @staticmethod
    def _session(directory):
        session = _BrowserSession(threading.Event(), 1)
        session.directory = Path(directory)
        session.proc = MagicMock()
        session.proc.poll.return_value = None
        return session

    def test_failed_write_terminates_child_before_clearing_in_request(self):
        with tempfile.TemporaryDirectory() as directory:
            session = self._session(directory)
            observed = []
            def terminate(proc):
                observed.append(session.in_request)
                proc.poll.return_value = -9
            with patch("hdusign.attendance.write_json", side_effect=OSError("disk full")), \
                    patch.object(AutoSigner, "_terminate_child", side_effect=terminate) as kill:
                with self.assertRaises(OSError):
                    session.submit("1234", time.time())
            kill.assert_called_once_with(session.proc)
            self.assertEqual(observed, [True])
            self.assertFalse(session.in_request)

    def test_timeout_and_result_read_errors_terminate_child(self):
        for phase in ("timeout", "invalid_json", "read_error"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                session = self._session(directory)
                result_path = Path(directory) / "result-1.json"
                if phase == "invalid_json":
                    result_path.write_text("{invalid JSON", encoding="utf-8")
                    error = ValueError
                elif phase == "read_error":
                    # Existence is seen by the waiter, but the file disappears
                    # before it is opened for the result read.
                    session._wait = MagicMock()
                    error = OSError
                else:
                    session._wait = MagicMock(side_effect=subprocess.TimeoutExpired("wait", 1))
                    error = subprocess.TimeoutExpired
                with patch.object(AutoSigner, "_terminate_child") as kill:
                    with self.assertRaises(error):
                        session.submit("1234", time.time())
                kill.assert_called_once_with(session.proc)
                self.assertFalse(session.in_request)

    def test_failed_termination_retains_inflight_marker_and_original_error(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(StringIO()):
            session = self._session(directory)
            session._wait = MagicMock(side_effect=subprocess.TimeoutExpired("wait", 1))
            with patch.object(AutoSigner, "_terminate_child", side_effect=OSError("kill failed")):
                with self.assertRaises(subprocess.TimeoutExpired):
                    session.submit("1234", time.time())
            self.assertTrue(session.in_request)
            proc = session.proc
            with patch.object(AutoSigner, "_terminate_child") as kill:
                session.close()
            kill.assert_called_once_with(proc)
            proc.wait.assert_not_called()

    def test_confirmed_result_keeps_prepared_child_reusable(self):
        with tempfile.TemporaryDirectory() as directory:
            session = self._session(directory)
            result_path = Path(directory) / "result-1.json"
            write_json(result_path, {"status": "success", "sign_code": "1234", "message": "OK"})
            session._wait = MagicMock()
            with patch.object(AutoSigner, "_terminate_child") as kill:
                result = session.submit("1234", time.time())
            self.assertEqual(result.status, "success")
            kill.assert_not_called()
            self.assertFalse(session.in_request)


class SignControlFlowNotificationTests(unittest.TestCase):
    def test_prepared_success_survives_failing_notification_callback(self):
        notify = MagicMock(side_effect=RuntimeError("notification unavailable"))
        signer = AutoSigner(notify)
        with tempfile.TemporaryDirectory() as directory:
            session = _BrowserSession(signer._stop, 1, on_verification=signer._notify_verification)
            session.directory = Path(directory)
            session.proc = MagicMock()
            session.proc.poll.return_value = None
            signer._session = session
            write_json(session.directory / "progress-1.json",
                       {"status": "verification_required", "sign_code": "1234"})
            def finish(_):
                notify.assert_called_once()
                SignResult("success", "1234", "OK").write(session.directory / "result-1.json")
            with patch.object(signer._stop, "wait", side_effect=finish), \
                    patch.object(signer, "_notify_result", wraps=signer._notify_result) as result, \
                    redirect_stdout(StringIO()) as output:
                signer._sign_one("1234")
            self.assertEqual(result.call_args.args[0].status, "success")
            self.assertEqual(notify.call_count, 2)
            self.assertIs(signer._session, session)
            self.assertFalse(session.in_request)
        self.assertIn("签到通知失败", output.getvalue())

    def test_cold_start_success_survives_failing_notification_callback(self):
        notify = MagicMock(side_effect=RuntimeError("notification unavailable"))
        signer = AutoSigner(notify)
        def run_child(command, *, progress):
            progress_path = Path(command[command.index("--progress-file") + 1])
            write_json(progress_path, {"status": "verification_required", "sign_code": "1234"})
            progress()
            notify.assert_called_once()
            result_path = Path(command[command.index("--result-file") + 1])
            write_json(result_path, {"status": "success", "sign_code": "1234", "message": "OK"})
            return subprocess.CompletedProcess(command, 0, "", "")
        with patch.object(signer, "_run_child", side_effect=run_child), \
                patch.object(signer, "_notify_result", wraps=signer._notify_result) as result, \
                redirect_stdout(StringIO()) as output:
            signer._sign_one("1234")
        self.assertEqual(result.call_args.args[0].status, "success")
        self.assertEqual(notify.call_count, 2)
        self.assertIn("签到通知失败", output.getvalue())


class ChildCancellationTests(unittest.TestCase):
    @staticmethod
    def child(entered):
        child = MagicMock()
        child.returncode = None
        child.__enter__.return_value = child
        child.poll.side_effect = lambda: (entered.set() or child.returncode)
        child.kill.side_effect = lambda: setattr(child, "returncode", -9)
        return child

    def test_stop_terminates_running_child_without_starting_pending_code(self):
        stop, entered = threading.Event(), threading.Event()
        child = self.child(entered)
        notifications = []
        signer = AutoSigner(lambda *args: notifications.append(args), stop=stop)
        self.addCleanup(signer.close)
        self.addCleanup(stop.set)
        with patch("hdusign.attendance.subprocess.Popen", return_value=child) as launch, \
                patch.object(signer, "_terminate_child", side_effect=lambda process: process.kill()) as terminate, \
                redirect_stdout(StringIO()):
            signer.submit("1234")
            self.assertTrue(entered.wait(2))
            signer.submit("5678")
            stop.set()
            signer.close()
        self.assertEqual(launch.call_count, 1)
        terminate.assert_called_once_with(child)
        self.assertEqual(child.returncode, -9)
        self.assertEqual(notifications, [])

    def test_deadline_terminates_child_and_reports_unknown_without_retry(self):
        child = self.child(threading.Event())
        notifications = []
        signer = AutoSigner(lambda *args: notifications.append(args), timeout_s=0.01)
        with patch("hdusign.attendance.subprocess.Popen", return_value=child) as launch, \
                patch.object(signer, "_terminate_child", side_effect=lambda process: process.kill()) as terminate, \
                redirect_stdout(StringIO()):
            signer._sign_one("1234")
        self.assertEqual(launch.call_count, 1)
        terminate.assert_called_once_with(child)
        self.assertEqual(len(notifications), 1)
        self.assertIn("结果未知", notifications[0][0])

    def test_pre_cancelled_session_never_launches_a_child(self):
        stop = threading.Event()
        stop.set()
        signer = AutoSigner(lambda *_: None, stop=stop)
        with patch("hdusign.attendance.subprocess.Popen") as launch:
            self.assertFalse(signer.submit("1234"))
            signer._sign_one("1234")
            signer.close()
        launch.assert_not_called()

    def test_real_child_output_and_exit_code_are_preserved(self):
        signer = AutoSigner(lambda *_: None, timeout_s=5)
        result = signer._run_child([
            sys.executable, "-X", "utf8", "-c",
            "import sys; print('课堂测试'); print('sample error', file=sys.stderr); sys.exit(7)",
        ])
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout.strip(), "课堂测试")
        self.assertEqual(result.stderr.strip(), "sample error")

    @unittest.skipUnless(sys.platform == "win32", "Windows process-tree cleanup")
    def test_cancel_reaps_child_tree_and_preserves_an_unrelated_process(self):
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        stop = threading.Event()
        signer = AutoSigner(lambda *_: None, timeout_s=10, stop=stop)
        results, errors = [], []
        descendant_handle = None
        runner = None
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        sleeper = [sys.executable, "-c", "import time; time.sleep(15)"]
        control = subprocess.Popen(sleeper, creationflags=flags,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "descendant.pid"
            script = (
                "import subprocess, sys, time\n"
                "from pathlib import Path\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(15)'], "
                "creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))\n"
                "Path(sys.argv[1]).write_text(str(child.pid))\n"
                "time.sleep(15)\n"
            )

            def run():
                try:
                    results.append(signer._run_child([sys.executable, "-c", script, str(marker)]))
                except Exception as exc:
                    errors.append(exc)

            try:
                runner = threading.Thread(target=run, daemon=True)
                runner.start()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if marker.exists() and marker.read_text().strip():
                        break
                    stop.wait(0.01)
                self.assertTrue(marker.exists(), f"Child did not start: {errors}")
                # Hold the actual child handle, so PID reuse cannot affect cleanup.
                descendant_handle = kernel.OpenProcess(0x00100001, False, int(marker.read_text()))
                self.assertTrue(descendant_handle, ctypes.get_last_error())
                stop.set()
                runner.join(timeout=8)
                self.assertFalse(runner.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(results, [None])
                self.assertEqual(kernel.WaitForSingleObject(descendant_handle, 2000), 0)
                self.assertIsNone(control.poll())
            finally:
                stop.set()
                if runner is not None:
                    runner.join(timeout=8)
                if descendant_handle:
                    if kernel.WaitForSingleObject(descendant_handle, 0) == 258:
                        kernel.TerminateProcess(descendant_handle, 1)
                    kernel.CloseHandle(descendant_handle)
                control.kill()
                control.wait()


if __name__ == "__main__":
    unittest.main()
