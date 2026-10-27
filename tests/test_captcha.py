"""Manual verification continuation with virtual time; no school requests."""
from contextlib import ExitStack, redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from hdusign import browser
from hdusign.attendance import AutoSigner, SignResult, _BrowserSession, verification_progress, write_json


ENDPOINT = "https://skl.hdu.edu.cn/api/ali-nvc/captcha-verify?code=1234"


class Page:
    def __init__(self):
        self.now = 0
        self.captcha = False
        self.stopped = False
        self.handlers = {}
        self.scheduled = []
        self.events = []
        self.bring_to_front = Mock()
        self.query_selector_all = Mock(return_value=[])

    def on(self, event, callback):
        self.events.append("arm:" + event)
        self.handlers[event] = callback

    def remove_listener(self, event, callback):
        assert self.handlers.pop(event) == callback
        self.events.append("remove:" + event)

    def emit(self, event, value):
        if event in self.handlers:
            self.handlers[event](value)

    def reply(self, payload, *, url=ENDPOINT, fresh=True, status=200):
        request = Mock(url=url, method="POST")
        if fresh:
            self.emit("request", request)
        response = Mock(url=url, request=request, status=status)
        response.json.return_value = payload
        self.emit("response", response)

    def later(self, seconds, callback):
        self.scheduled.append((seconds, callback))
        self.scheduled.sort(key=lambda item: item[0])

    def wait_for_timeout(self, milliseconds):
        self.now += milliseconds / 1000
        while self.scheduled and self.scheduled[0][0] <= self.now:
            _, callback = self.scheduled.pop(0)
            callback()


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.page = Page()
        self.stages = []
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(redirect_stdout(StringIO()))
        self.stack.enter_context(patch.object(browser.time, "monotonic", side_effect=lambda: self.page.now))
        self.stack.enter_context(patch.object(browser, "_captcha_visible", side_effect=lambda _: self.page.captcha))
        self.verify_click = self.stack.enter_context(patch.object(browser, "_click_verification", return_value=False))
        self.stack.enter_context(patch.object(browser, "open_sign_in", side_effect=lambda _: self.page.events.append("open")))
        self.click = self.stack.enter_context(patch.object(browser, "click_visible", side_effect=self.on_click))

    def on_click(self, page, selector, text, **kwargs):
        self.page.events.append("click:" + text)

    def sign(self):
        return browser.sign_with_code(None, self.page, "1234",
                                      on_stage=lambda name, when: self.stages.append(name),
                                      cancelled=lambda: self.page.stopped)

    def assert_clean(self):
        self.assertEqual(self.page.handlers, {})
        self.assertEqual(self.page.events.count("click:签到"), 1)

    def test_listeners_surround_only_submission_and_normal_success_stays_fast(self):
        self.page.later(.1, lambda: self.page.reply({"code": 200}))
        self.assertEqual(self.sign().status, "success")
        self.assertLess(self.page.now, 1)
        self.assertEqual(self.page.events[:8], ["open", "click:1", "click:2", "click:3", "click:4",
                                                "arm:request", "arm:response", "click:签到"])
        self.assertNotIn("verification_required", self.stages)
        self.assert_clean()

    def test_disabled_boost_does_not_add_random_typing_pauses(self):
        self.page.later(.1, lambda: self.page.reply({"code": 200}))
        with patch.object(browser.captcha_boost, "enabled_for", return_value=False), \
                patch.object(browser.random, "random", side_effect=AssertionError("disabled random delay")), \
                patch.object(browser.time, "sleep", side_effect=AssertionError("disabled typing pause")):
            self.assertEqual(self.sign().status, "success")

    def test_manual_handoff_cancels_boost_and_success_finishes_round(self):
        self.page.captcha = True
        self.page.later(1, lambda: self.page.reply({"code": 200}))
        with patch.object(browser.captcha_boost, "manual_takeover_for") as handoff, \
                patch.object(browser.captcha_boost, "finish_round_for") as finish:
            self.assertEqual(self.sign().status, "success")
        handoff.assert_called_with(self.page)
        finish.assert_called_once_with(self.page, success=True)

    def test_unsuccessful_round_discards_pending_manual_capture(self):
        self.page.later(.1, lambda: self.page.reply({"code": 400, "msg": "签到码错误"}))
        with patch.object(browser.captcha_boost, "finish_round_for") as finish:
            self.assertNotEqual(self.sign().status, "success")
        finish.assert_called_once_with(self.page, success=False)

    def test_visible_captcha_remains_open_beyond_old_twelve_second_limit(self):
        self.page.captcha = True
        self.page.later(25, lambda: self.page.reply({"code": 200, "msg": "签到成功"}))
        self.assertEqual(self.sign().status, "success")
        self.assertGreaterEqual(self.page.now, 25)
        self.assertEqual(self.stages.count("verification_required"), 1)
        self.page.bring_to_front.assert_called_once()
        self.assert_clean()

    def test_bring_to_front_failure_does_not_discard_the_result(self):
        self.page.captcha = True
        self.page.bring_to_front.side_effect = browser.PlaywrightError("window unavailable")
        self.page.later(1, lambda: self.page.reply({"code": 200}))
        self.assertEqual(self.sign().status, "success")
        self.assertEqual(self.stages.count("verification_required"), 1)
        self.assert_clean()

    def test_listener_cleanup_error_does_not_replace_confirmed_result(self):
        self.page.later(.1, lambda: self.page.reply({"code": 200}))
        with patch.object(self.page, "remove_listener", side_effect=browser.PlaywrightError("closed")) as remove:
            self.assertEqual(self.sign().status, "success")
        self.assertEqual([call.args[0] for call in remove.call_args_list], ["request", "response"])

    def test_single_click_can_complete_verification_without_manual_prompt(self):
        self.page.captcha = True
        self.verify_click.return_value = True
        self.page.later(1, lambda: self.page.reply({"code": 200}))
        self.assertEqual(self.sign().status, "success")
        self.verify_click.assert_called_once()
        self.assertEqual(self.stages.count("verification_clicked"), 1)
        self.assertNotIn("verification_required", self.stages)
        self.assert_clean()

    def test_single_click_is_not_repeated_if_challenge_remains(self):
        self.page.captcha = True
        self.verify_click.return_value = True
        self.page.later(5, lambda: setattr(self.page, "stopped", True))
        with self.assertRaises(InterruptedError):
            self.sign()
        self.verify_click.assert_called_once()
        self.assertEqual(self.stages.count("verification_required"), 1)
        self.assert_clean()

    def test_challenge_response_and_failed_attempt_do_not_end_attendance(self):
        self.page.later(.1, lambda: self.page.reply({"code": 400, "msg": "请完成验证码"}))
        self.page.later(4, lambda: self.page.reply({"code": 400, "msg": "验证码错误，请重试"}))
        self.page.later(20, lambda: self.page.reply({"code": 200}))
        self.assertEqual(self.sign().status, "success")
        self.assertEqual(self.stages.count("verification_required"), 1)
        self.assert_clean()

    def test_http_success_with_challenge_message_is_still_intermediate(self):
        self.page.later(.1, lambda: self.page.reply({"code": 200, "msg": "请完成验证码"}))
        self.page.later(20, lambda: self.page.reply({"code": 200, "msg": "签到成功"}))
        self.assertEqual(self.sign().status, "success")
        self.assertEqual(self.stages.count("verification_required"), 1)
        self.assert_clean()

    def test_challenge_shown_after_generic_response_is_given_time_to_render(self):
        self.page.later(.1, lambda: self.page.reply({"code": 400, "msg": "需要校验"}))
        self.page.later(.3, lambda: setattr(self.page, "captcha", True))
        self.page.later(20, lambda: self.page.reply({"code": 200}))
        self.assertEqual(self.sign().status, "success")
        self.assertGreaterEqual(self.page.now, 20)

    def test_missing_widget_does_not_claim_that_a_captcha_is_visible(self):
        self.page.later(20, lambda: self.page.reply({"code": 200}))
        self.assertEqual(self.sign().status, "success")
        self.assertNotIn("verification_required", self.stages)
        self.assert_clean()

    def test_old_same_code_and_unrelated_successes_cannot_satisfy_submission(self):
        self.page.later(.1, lambda: self.page.reply({"code": 200}, fresh=False))
        self.page.later(.2, lambda: self.page.reply({"code": 200}, url=ENDPOINT.replace("1234", "4321")))
        self.page.later(.3, lambda: self.page.reply({"code": 200}, url="https://skl.hdu.edu.cn/api/login"))
        self.page.later(.4, lambda: self.page.reply({"code": 200}, url=ENDPOINT.replace("skl.hdu.edu.cn", "example.com")))
        self.assertEqual(self.sign().status, "unknown")
        self.assertGreaterEqual(self.page.now, 120)
        self.assert_clean()

    def test_disappearing_challenge_does_not_mean_attendance_success(self):
        self.page.captcha = True
        self.page.later(10, lambda: setattr(self.page, "captcha", False))
        self.assertEqual(self.sign().status, "unknown")
        self.assertNotIn("response", self.stages)
        self.assert_clean()

    def test_repeated_challenges_do_not_extend_deadline(self):
        for when in (1, 50, 110):
            self.page.later(when, lambda: self.page.reply({"code": 400, "msg": "安全验证失败"}))
        self.assertEqual(self.sign().status, "unknown")
        self.assertLess(self.page.now, 121)
        self.assertEqual(self.stages.count("verification_required"), 1)
        self.assert_clean()

    def test_stop_during_verification_removes_listeners_without_resubmitting(self):
        self.page.captcha = True
        self.page.later(2, lambda: setattr(self.page, "stopped", True))
        with self.assertRaises(InterruptedError):
            self.sign()
        self.assertLess(self.page.now, 2.2)
        self.assert_clean()

    def test_attendance_rejection_after_verification_is_still_failure(self):
        self.page.captcha = True
        self.page.later(20, lambda: setattr(self.page, "captcha", False))
        self.page.later(20, lambda: self.page.reply({"code": 400, "msg": "签到码错误"}))
        self.assertEqual(self.sign().status, "failure")
        self.assertLess(self.page.now, 22)
        self.assert_clean()

    def test_rejection_is_preserved_when_widget_closes_after_response(self):
        self.page.captcha = True
        self.page.later(20, lambda: self.page.reply({"code": 400, "msg": "签到已结束"}))
        self.page.later(20.3, lambda: setattr(self.page, "captcha", False))
        self.assertEqual(self.sign().status, "failure")
        self.assertLess(self.page.now, 22)
        self.assert_clean()

    def test_failure_or_invalid_payload_without_captcha_finishes_promptly(self):
        for payload, expected in (({"code": 400, "msg": "签到未成功"}, "failure"), (None, "unknown")):
            with self.subTest(payload=payload):
                self.page = Page()
                self.page.later(.1, lambda: self.page.reply(payload))
                self.assertEqual(self.sign().status, expected)
                self.assertLess(self.page.now, 2)
                self.assert_clean()

    def test_click_timeout_cleans_up_listeners_and_does_not_retry(self):
        def click(page, selector, text, **kwargs):
            self.on_click(page, selector, text)
            if text == "签到":
                raise browser.PlaywrightTimeoutError("timeout")
        self.click.side_effect = click
        self.assertEqual(self.sign().status, "unknown")
        self.assert_clean()


class VerificationProgressTests(unittest.TestCase):
    def test_prepared_request_flag_is_cleared_when_result_file_is_invalid(self):
        with tempfile.TemporaryDirectory() as directory:
            session = _BrowserSession(threading.Event(), 2)
            session.directory = Path(directory)
            session.proc = Mock()
            session.proc.poll.return_value = None
            (session.directory / "result-1.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(ValueError):
                session.submit("1234", 0)
            self.assertFalse(session.in_request)

    def test_progress_is_once_per_request_and_rejects_wrong_codes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "progress.json"
            notify = Mock()
            poll = verification_progress(path, "1234", notify)
            for data in ([], {"status": "success", "sign_code": "1234"},
                         {"status": "verification_required", "sign_code": "4321"}):
                write_json(path, data)
                poll()
            notify.assert_not_called()
            write_json(path, {"status": "verification_required", "sign_code": "1234"})
            poll()
            poll()
            notify.assert_called_once_with("1234")

    def test_prepared_browser_reports_progress_before_result_and_per_request(self):
        with tempfile.TemporaryDirectory() as directory:
            notify = Mock()
            session = _BrowserSession(threading.Event(), 2, on_verification=notify)
            session.directory = Path(directory)
            session.proc = Mock()
            session.proc.poll.return_value = None
            for sequence in (1, 2):
                write_json(session.directory / f"progress-{sequence}.json",
                           {"status": "verification_required", "sign_code": "1234"})
                def finish(_):
                    self.assertEqual(notify.call_count, sequence)
                    SignResult("success", "1234", "OK").write(session.directory / f"result-{sequence}.json")
                with patch.object(session.stop, "wait", side_effect=finish):
                    self.assertEqual(session.submit("1234", 0).status, "success")
            self.assertEqual(notify.call_count, 2)

    def test_cold_browser_polls_progress_before_child_exits(self):
        notify = Mock()
        signer = AutoSigner(notify)
        proc = Mock(returncode=0)
        proc.__enter__ = Mock(return_value=proc)
        proc.__exit__ = Mock(return_value=False)
        proc.poll.side_effect = [None, 0, 0]
        progress = Mock()
        with patch("hdusign.attendance.subprocess.Popen", return_value=proc):
            signer._run_child(["offline-test"], progress=progress)
        progress.assert_called_once()

    def test_cold_browser_passes_private_progress_file_and_forwards_prompt(self):
        notify = Mock()
        signer = AutoSigner(notify)
        def run(command, *, progress):
            path = Path(command[command.index("--progress-file") + 1])
            write_json(path, {"status": "verification_required", "sign_code": "1234"})
            progress()
            self.assertIn("等待验证", notify.call_args.args[0])
            SignResult("success", "1234", "OK").write(Path(command[command.index("--result-file") + 1]))
            return Mock(returncode=0, stdout="", stderr="")
        with patch.object(signer, "_run_child", side_effect=run), redirect_stdout(StringIO()):
            signer._sign_one("1234")
        self.assertEqual(notify.call_count, 2)
        self.assertIn("签到成功", notify.call_args.args[0])

    def test_no_verification_notification_after_stop(self):
        notify = Mock()
        signer = AutoSigner(notify)
        signer._stop.set()
        signer._notify_verification("1234")
        notify.assert_not_called()


class LoginAttemptTests(unittest.TestCase):
    def test_unsubmitted_login_form_is_left_for_user_without_repeated_fills(self):
        page = Mock(url="https://sso.hdu.edu.cn/login")
        now = 0
        def wait(milliseconds):
            nonlocal now
            now += milliseconds / 1000
            if now >= 1:
                page.url = browser.START
        page.wait_for_timeout.side_effect = wait
        page.evaluate.return_value = "fixture-token"
        with patch.object(browser.time, "monotonic", side_effect=lambda: now), \
                patch.object(browser, "try_sso_login", return_value=False) as login:
            self.assertEqual(browser.ensure_logged_in(page, {}, timeout_s=5), "fixture-token")
        login.assert_called_once_with(page, {})


if __name__ == "__main__":
    unittest.main()
