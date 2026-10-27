"""Browser preparation and request isolation without contacting the school."""
from contextlib import ExitStack, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from hdusign import browser
from hdusign.__main__ import main as app_main
from hdusign.attendance import AutoSigner, SignResult, _BrowserSession, write_json


def wait_for(predicate, seconds=3):
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Timed out waiting for test state")
        time.sleep(.01)


class PreparedSchedulerTests(unittest.TestCase):
    def test_system_monitor_prepares_before_loading_models_and_closes_on_failure(self):
        from hdusign import monitor
        signer = Mock()
        with patch.object(monitor, "LoopbackSource"), \
                patch.object(monitor, "make_alerter"), \
                patch.object(monitor, "make_auto_signer", return_value=signer), \
                patch.object(monitor, "build_matchers", return_value=[]), \
                patch.object(monitor, "make_watcher"), \
                patch.object(monitor, "make_engine") as engine, redirect_stdout(StringIO()):
            def fail(cfg):
                signer.prepare.assert_called_once()
                signer.submit.assert_not_called()
                raise RuntimeError("model unavailable")
            engine.side_effect = fail
            with self.assertRaisesRegex(RuntimeError, "model unavailable"):
                monitor.cmd_run({})
        signer.close.assert_called_once()

    def test_disabled_prewarm_does_not_launch_a_browser(self):
        signer = AutoSigner(Mock(), prewarm_browser=False)
        with patch("hdusign.attendance._BrowserSession") as session:
            self.assertFalse(signer.prepare())
            signer.close()
        session.assert_not_called()

    def test_preparation_does_not_submit_and_two_codes_reuse_one_session(self):
        ready, first, second = threading.Event(), threading.Event(), threading.Event()
        session = Mock()
        session.open.side_effect = ready.set
        def submit(code, when):
            (first if code == "8348" else second).set()
            return SignResult("success", code, "OK")
        session.submit.side_effect = submit
        signer = AutoSigner(Mock(), prewarm_browser=True)
        self.addCleanup(signer.close)
        with patch("hdusign.attendance._BrowserSession", return_value=session) as factory, redirect_stdout(StringIO()):
            self.assertTrue(signer.prepare())
            self.assertTrue(ready.wait(2))
            signer.prepare()
            session.submit.assert_not_called()
            signer.submit("8348")
            self.assertTrue(first.wait(2))
            signer.submit("7802")
            self.assertTrue(second.wait(2))
            signer.close(cancel=False)
        factory.assert_called_once()
        session.open.assert_called_once()
        session.close.assert_called_once()
        self.assertEqual([call.args[0] for call in session.submit.call_args_list], ["8348", "7802"])

    def test_unknown_prepared_submission_is_not_retried_in_another_browser(self):
        session = Mock()
        session.submit.side_effect = subprocess.TimeoutExpired("test", .1)
        notify = Mock()
        signer = AutoSigner(notify)
        signer._session = session
        with patch.object(signer, "_run_child") as cold, redirect_stdout(StringIO()):
            signer._sign_one("1234")
        cold.assert_not_called()
        session.close.assert_called_once()
        self.assertIn("结果未知", notify.call_args.args[0])

    def test_failed_preparation_does_not_block_the_first_confirmed_code(self):
        failed = threading.Event()
        session = Mock()
        def fail():
            failed.set()
            raise RuntimeError("simulated page failure")
        session.open.side_effect = fail
        signer = AutoSigner(Mock(), prewarm_browser=True)
        self.addCleanup(signer.close)
        with patch("hdusign.attendance._BrowserSession", return_value=session) as factory, \
                patch.object(signer, "_sign_one") as sign, redirect_stdout(StringIO()):
            signer.prepare()
            self.assertTrue(failed.wait(2))
            signer.submit("1234")
            signer.close(cancel=False)
        factory.assert_called_once()
        sign.assert_called_once_with("1234")
        session.submit.assert_not_called()
        session.close.assert_called_once()

    def test_stop_interrupts_preparation_without_dispatching_a_pending_code(self):
        stop, entered = threading.Event(), threading.Event()
        session = Mock()
        def opening():
            entered.set()
            stop.wait(2)
            raise InterruptedError("stopped")
        session.open.side_effect = opening
        signer = AutoSigner(Mock(), stop=stop, prewarm_browser=True)
        self.addCleanup(signer.close)
        self.addCleanup(stop.set)
        with patch("hdusign.attendance._BrowserSession", return_value=session), redirect_stdout(StringIO()):
            signer.prepare()
            self.assertTrue(entered.wait(2))
            signer.submit("1234")
            signer.close()
        session.submit.assert_not_called()
        signer.notify.assert_not_called()
        session.close.assert_called_once()


class WorkerProtocolTests(unittest.TestCase):
    def test_cancellation_during_typing_never_clicks_submit(self):
        stopped = False
        clicks = []
        def click(page, selector, text, **kwargs):
            nonlocal stopped
            clicks.append(text)
            if text == "2":
                stopped = True
        with patch.object(browser, "open_sign_in"), \
                patch.object(browser, "click_visible", side_effect=click), redirect_stdout(StringIO()):
            with self.assertRaises(InterruptedError):
                browser.sign_with_code(None, Mock(), "1234", cancelled=lambda: stopped)
        self.assertEqual(clicks, ["1", "2"])

    def test_closing_an_inflight_request_kills_it_without_a_grace_period(self):
        session = _BrowserSession(threading.Event(), 1)
        session.proc = Mock()
        session.proc.poll.return_value = None
        session.in_request = True
        proc = session.proc
        with patch.object(AutoSigner, "_terminate_child") as terminate:
            session.close()
        terminate.assert_called_once_with(proc)
        proc.wait.assert_not_called()

    def test_stop_file_failure_still_terminates_the_browser(self):
        session = _BrowserSession(threading.Event(), 1)
        session.directory = Path("unused-test-directory")
        session.proc = Mock()
        session.proc.poll.return_value = None
        proc = session.proc
        with patch("hdusign.attendance.write_json", side_effect=OSError("unavailable")), \
                patch.object(AutoSigner, "_terminate_child") as terminate:
            session.close()
        terminate.assert_called_once_with(proc)
        proc.wait.assert_not_called()

    def test_invalid_optional_timing_does_not_discard_a_confirmed_result(self):
        with tempfile.TemporaryDirectory() as temp, redirect_stdout(StringIO()):
            session = _BrowserSession(threading.Event(), 1)
            session.directory = Path(temp)
            session.proc = Mock()
            session.proc.poll.return_value = None
            SignResult("success", "1234", "OK").write(session.directory / "result-1.json")
            (session.directory / "timing-1.json").write_text("broken", encoding="utf-8")
            self.assertEqual(session.submit("1234", time.time()).status, "success")
            self.assertFalse(session.in_request)

    def test_browser_waits_for_confirmed_requests_and_clears_page_between_codes(self):
        with tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
            folder = Path(temp)
            calls, failures = [], []
            stack.enter_context(patch.object(browser, "ensure_logged_in"))
            stack.enter_context(patch.object(browser, "prepared_page_ready", return_value=True))
            ready = stack.enter_context(patch.object(browser, "open_sign_in"))
            def sign(ctx, page, code, *, on_stage, cancelled):
                calls.append(code)
                for stage in ("page_ready", "digits_entered", "submit", "response"):
                    on_stage(stage, time.time())
                on_stage("verification_required", time.time())
                progress = json.loads((folder / f"progress-{len(calls)}.json").read_text(encoding="utf-8"))
                self.assertEqual(progress, {"status": "verification_required", "sign_code": code})
                return SignResult("success", code, "OK")
            stack.enter_context(patch.object(browser, "sign_with_code", side_effect=sign))
            def run():
                try:
                    browser.serve_prepared(None, Mock(), {}, folder)
                except Exception as exc:
                    failures.append(exc)
            thread = threading.Thread(target=run)
            thread.start()
            try:
                wait_for(lambda: (folder/'ready.json').exists())
                self.assertEqual(calls, [])
                for sequence, code in ((1, "8348"), (2, "7802")):
                    write_json(folder/f'request-{sequence}.json', {"id":sequence,"code":code,"detected_at":time.time()})
                    wait_for(lambda: (folder/f'result-{sequence}.json').exists())
                    self.assertEqual(SignResult.read(folder/f'result-{sequence}.json', code).status, "success")
                self.assertEqual(calls, ["8348", "7802"])
                self.assertGreaterEqual(ready.call_count, 2)
            finally:
                write_json(folder/'stop.json', {})
                thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(failures, [])

    def test_invalid_code_is_rejected_without_typing_or_submitting(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(browser, "ensure_logged_in"), \
                patch.object(browser, "open_sign_in"), patch.object(browser, "sign_with_code") as sign:
            folder = Path(temp)
            write_json(folder/'request-1.json', {"id":1, "code":"12345"})
            with self.assertRaises(ValueError):
                browser.serve_prepared(None, Mock(), {}, folder)
            sign.assert_not_called()

    def test_expired_prewarmed_page_reports_failure_without_typing_or_retry(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(browser, "ensure_logged_in") as login, \
                patch.object(browser, "open_sign_in"), \
                patch.object(browser, "prepared_page_ready", return_value=False), \
                patch.object(browser, "sign_with_code") as sign:
            folder = Path(temp)
            write_json(folder / "request-1.json", {"id": 1, "code": "1234"})
            browser.serve_prepared(None, Mock(), {}, folder)
            result = SignResult.read(folder / "result-1.json", "1234")
            self.assertEqual(result.status, "failure")
            self.assertIn("本次未输入或提交", result.message)
            sign.assert_not_called()
            login.assert_called_once()

    def test_request_id_mismatch_is_rejected_without_submitting(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(browser, "ensure_logged_in"), \
                patch.object(browser, "open_sign_in"), patch.object(browser, "sign_with_code") as sign:
            folder = Path(temp)
            write_json(folder/'request-1.json', {"id":2, "code":"1234"})
            with self.assertRaises(ValueError):
                browser.serve_prepared(None, Mock(), {}, folder)
            sign.assert_not_called()

    def test_frozen_worker_entrypoint_preserves_exchange_directory(self):
        with patch("os.chdir"), patch.object(browser, "sign_worker", return_value=0) as worker:
            self.assertEqual(app_main(["--sign-worker", "--exchange-dir", "private-dir"]), 0)
        worker.assert_called_once_with(["--exchange-dir", "private-dir"])

    def test_real_child_uses_new_result_ids_even_when_same_code_is_sent_twice(self):
        script = """
import json, pathlib, sys, time
p=pathlib.Path(sys.argv[1]); seen=0
def write(path, data):
    tmp=path.with_suffix('.tmp'); tmp.write_text(json.dumps(data)); tmp.replace(path)
write(p/'ready.json', {'status':'ready'})
while not (p/'stop.json').exists():
    request=p/f'request-{seen+1}.json'
    if request.exists():
        data=json.loads(request.read_text())
        if data['id']>seen:
            seen=data['id']
            write(p/f'result-{seen}.json', {'status':'success','sign_code':data['code'],'message':str(seen)})
    time.sleep(.01)
"""
        session = _BrowserSession(threading.Event(), 5)
        self.addCleanup(session.close)
        child_output = None
        original_popen = subprocess.Popen

        def start_child(*args, **kwargs):
            nonlocal child_output
            child_output = kwargs["stdout"]
            return original_popen(*args, **kwargs)

        with patch("hdusign.attendance.browser_command", side_effect=lambda *args: [
                sys.executable, "-X", "utf8", "-c", script, args[-1]]), \
                patch("hdusign.attendance.subprocess.Popen", side_effect=start_child):
            try:
                session.open()
                process, folder = session.proc, session.directory
                self.assertEqual(session.submit("1234", time.time()).message, "1")
                self.assertEqual(session.submit("1234", time.time()).message, "2")
                session.close()
            except Exception as exc:
                # Preserve the fake child's traceback before cleanup closes its log.
                output = "(no child output)"
                if child_output is not None and not child_output.closed:
                    child_output.seek(0)
                    output = child_output.read().decode("utf-8", errors="replace") or output
                returncode = session.proc.poll() if session.proc is not None else None
                raise AssertionError(
                    f"Offline fake child returncode={returncode}; stdout/stderr:\n{output}"
                ) from exc
        self.assertIsNotNone(process.poll())
        self.assertFalse(folder.exists())


if __name__ == "__main__":
    unittest.main()
