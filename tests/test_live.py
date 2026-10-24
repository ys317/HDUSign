"""Live-platform contracts using fake responses and browser pages only."""
from __future__ import annotations

import copy
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import threading
import traceback
import unittest
from unittest.mock import MagicMock, Mock, patch
from urllib.parse import parse_qs, urlsplit

import requests

from hdusign import browser, live


ORIGIN = "https://course.hdu.edu.cn"
PAGE = ORIGIN + "/#/play-center?courseId=123&teclId=45&liveId=123&target=live"
VPN = "https://https-course-hdu-edu-cn-443.webvpn.hdu.edu.cn"
NOW = 1_789_351_200.0


def cookie(name="fixture-session", domain="course.hdu.edu.cn", **extra):
    return {"name": name, "value": "fixture-cookie-value", "domain": domain,
            "path": "/", "secure": True, "expires": -1, **extra}


def live_payload():
    return {"status": 200, "code": 200, "ok": True, "timestamp": NOW * 1000,
            "data": {"id": 123, "lvcrLiveStatus": 1,
                     "liveStartTime": (NOW - 300) * 1000,
                     "liveEndTime": (NOW + 1800) * 1000,
                     "courseDeviceViewDtoList": [
                         {"chanNameMainPlayUrl": "https://video.hdu.edu.cn/live/class.flv?quality=low",
                          "mainTokenStr": "fixture-media+/value=", "deviViewNum": 1}]}}


class LivePageTests(unittest.TestCase):
    def test_known_live_routes_and_origins(self):
        for origin in (ORIGIN, VPN):
            for route in ("/play-center", "/play-live"):
                with self.subTest(origin=origin, route=route):
                    url = origin + "/#" + route + "?courseId=00123&target=live"
                    self.assertEqual(live.parse_live_page(url), (origin, "123"))
                    self.assertEqual(live.LiveClient.parse_live_page(url), (origin, "123"))

    def test_replay_share_ambiguous_and_untrusted_urls_are_rejected(self):
        urls = [None, "", "file:///tmp/live.flv", PAGE.replace("https:", "http:"),
                PAGE.replace("course.hdu.edu.cn", "course.hdu.edu.cn.example.com"),
                PAGE.replace("course.hdu.edu.cn", "user:fixture-password@course.hdu.edu.cn"),
                PAGE.replace("course.hdu.edu.cn", "course.hdu.edu.cn:444"),
                PAGE.replace("target=live", "target=video"),
                PAGE.replace("target=live", "target=live&target=video"),
                PAGE.replace("courseId=123", "courseId=123&courseId=456"),
                PAGE.replace("courseId=123", "courseId=abc"),
                PAGE.replace("courseId=123", "courseId=0"),
                PAGE.replace("courseId=123", "courseId=-1"),
                PAGE.replace("/play-center", "/live-share"),
                PAGE.replace("courseId=123", "courseId="),
                PAGE + "\r\ninjected", ORIGIN + "/#//[bad"]
        for url in urls:
            with self.subTest(url=url):
                with self.assertRaises(live.LiveError) as caught:
                    live.parse_live_page(url)
                self.assertNotIn("fixture-password", str(caught.exception))
                self.assertFalse(caught.exception.retryable)


class LiveClientTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.root_patch = patch.object(live, "application_root", return_value=self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.clock_patch = patch.object(live.time, "time", return_value=NOW)
        self.clock_patch.start()
        self.addCleanup(self.clock_patch.stop)
        self.request_patch = patch.object(requests.Session, "request",
                                          side_effect=AssertionError("Network is forbidden in this test"))
        self.request_patch.start()
        self.addCleanup(self.request_patch.stop)
        self.response = Mock()
        self.response.status_code = 200
        self.response.json.return_value = live_payload()
        self.get_patch = patch.object(requests.Session, "get", autospec=True,
                                      return_value=self.response)
        self.get = self.get_patch.start()
        self.addCleanup(self.get_patch.stop)
        self.save_auth()

    def save_auth(self, **changes):
        auth = {"version": 1, "origin": ORIGIN, "cookies": [cookie()],
                "jwt_token": "fixture-jwt"}
        auth.update(changes)
        (self.root / "live_session.json").write_text(json.dumps(auth), encoding="utf-8")

    def client(self, page=PAGE):
        client = live.LiveClient(page, {"browser": {"bypass_proxy": True}})
        self.addCleanup(client.close)
        return client

    def test_live_get_builds_one_flv_stream_and_encodes_only_media_token(self):
        client = self.client()
        stream = client.resolve()
        args, kwargs = self.get.call_args
        self.assertEqual(args[1], ORIGIN + live._API + "/v1/course_vod_videoinfos")
        self.assertEqual(kwargs["params"], {"courseId": "123", "playType": 2})
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["timeout"], (10, 15))
        self.assertFalse(client._session.trust_env)
        self.assertEqual(client._session.headers["jwt-token"], "fixture-jwt")
        self.assertEqual(client._session.cookies.get("fixture-session"), "fixture-cookie-value")
        query = parse_qs(urlsplit(stream.url).query)
        self.assertEqual(query, {"quality": ["low"], "account_token": ["fixture-media+/value="]})
        self.assertEqual(set(stream.headers), {"User-Agent", "Referer"})
        self.assertEqual(stream.headers["Referer"], ORIGIN + "/")
        self.assertNotIn("fixture", repr(stream))
        self.assertNotIn("https://", repr(stream))
        self.get.assert_called_once()
        self.response.close.assert_called_once()

    def test_no_platform_cookie_or_jwt_goes_to_media_even_initially_same_origin(self):
        self.save_auth(cookies=[cookie(), cookie("wide", ".hdu.edu.cn")])
        for url in (ORIGIN + "/live.flv", "https://video.hdu.edu.cn/live.flv",
                    "http://video.hdu.edu.cn/live.flv"):
            with self.subTest(url=url):
                self.response.json.return_value["data"]["courseDeviceViewDtoList"][0][
                    "chanNameMainPlayUrl"] = url
                stream = self.client().resolve()
                self.assertNotIn("Cookie", stream.headers)
                self.assertNotIn("jwt-token", stream.headers)
                self.assertNotIn("fixture-cookie-value", str(stream.headers))

    def test_existing_media_token_is_replaced_without_corrupting_other_query_data(self):
        view = self.response.json.return_value["data"]["courseDeviceViewDtoList"][0]
        view["chanNameMainPlayUrl"] = (
            "https://video.hdu.edu.cn/live.flv?keep=a%20b&account_token=old&another=x%2By")
        result = self.client().resolve()
        self.assertIn("keep=a%20b", result.url)
        self.assertIn("another=x%2By", result.url)
        self.assertEqual(parse_qs(urlsplit(result.url).query)["account_token"],
                         ["fixture-media+/value="])

    def test_missing_site_login_does_not_read_skl_session_or_access_network(self):
        (self.root / "live_session.json").unlink()
        (self.root / "session_local.json").write_text(
            '{"x_auth_token":"fixture-unrelated-skl"}', encoding="utf-8")
        with self.assertRaises(live.LiveLoginRequired) as caught:
            self.client()
        self.assertFalse(caught.exception.retryable)
        self.get.assert_not_called()

    def test_wrong_origin_invalid_or_expired_login_is_rejected(self):
        for changes in ({"origin": VPN}, {"jwt_token": "\r\nfixture-jwt"},
                        {"jwt_token": "", "cookies": []},
                        {"jwt_token": "", "cookies": [cookie(expires=NOW - 1)]},
                        {"jwt_token": "", "cookies": [cookie(domain="skl.hdu.edu.cn")]}):
            with self.subTest(changes=changes):
                self.save_auth(**changes)
                with self.assertRaises(live.LiveLoginRequired):
                    self.client()
        self.get.assert_not_called()

    def test_session_loader_filters_foreign_and_malformed_cookies(self):
        self.save_auth(cookies=[cookie(), cookie("foreign", "skl.hdu.edu.cn"),
                                cookie("suffix", ".edu.cn"), cookie("bad\rname"),
                                cookie("injection", value="x; injected=y"),
                                cookie("expired", expires=NOW - 1)])
        client = self.client()
        self.assertEqual([c.name for c in client._session.cookies], ["fixture-session"])

    def test_vpn_login_must_belong_to_the_vpn_origin(self):
        self.save_auth(origin=VPN, cookies=[cookie(domain=".webvpn.hdu.edu.cn")])
        client = self.client(PAGE.replace(ORIGIN, VPN))
        client.resolve()
        self.assertTrue(self.get.call_args.args[1].startswith(VPN + live._API))

    def test_http_auth_and_business_denial_never_retry(self):
        cases = [(302, live_payload(), live.LiveLoginRequired, "登录"),
                 (401, live_payload(), live.LiveLoginRequired, "登录"),
                 (403, live_payload(), live.LiveError, "无权"),
                 (200, {"status": 401}, live.LiveLoginRequired, "登录"),
                 (200, {"status": 200, "code": 401}, live.LiveLoginRequired, "登录"),
                 (200, {"status": 403}, live.LiveError, "无权")]
        denied = live_payload()
        denied["data"]["lvcrLiveStatus"] = 0
        cases.append((200, denied, live.LiveError, "无权"))
        busy_but_denied = copy.deepcopy(denied)
        busy_but_denied["status"] = 503
        cases.append((200, busy_but_denied, live.LiveError, "无权"))
        cases.append((200, {"status": 503, "code": 401}, live.LiveLoginRequired, "登录"))
        cases.append((200, {"status": 503, "code": 403}, live.LiveError, "无权"))
        for http, body, error, message in cases:
            with self.subTest(http=http, body=body):
                self.get.reset_mock()
                self.response.status_code = http
                self.response.json.return_value = body
                with self.assertRaisesRegex(error, message) as caught:
                    self.client().resolve()
                self.assertFalse(caught.exception.retryable)
                self.get.assert_called_once()

    def test_transport_failures_are_retryable_without_leaking_errors_or_retrying(self):
        errors = (requests.ConnectionError, requests.ConnectTimeout, requests.ReadTimeout,
                  requests.exceptions.ProxyError, requests.exceptions.ChunkedEncodingError)
        for error in errors:
            with self.subTest(error=error.__name__):
                self.get.reset_mock()
                self.get.side_effect = error("fixture-sensitive-network-error")
                client = self.client()
                with self.assertRaises(live.LiveTransientError) as caught:
                    client.resolve()
                self.assertTrue(caught.exception.retryable)
                self.assertIsNone(caught.exception.__context__)
                self.assertNotIn("fixture-sensitive", str(caught.exception))
                self.assertEqual(client.course_id, "123")
                self.get.assert_called_once()

    def test_request_configuration_errors_are_not_retryable(self):
        errors = (requests.exceptions.InvalidURL, requests.exceptions.InvalidSchema,
                  requests.exceptions.InvalidHeader, requests.exceptions.InvalidProxyURL,
                  requests.exceptions.SSLError, requests.RequestException, ValueError)
        for error in errors:
            with self.subTest(error=error.__name__):
                self.get.reset_mock()
                self.get.side_effect = error("fixture-sensitive-config-error")
                with self.assertRaises(live.LiveError) as caught:
                    self.client().resolve()
                self.assertFalse(caught.exception.retryable)
                self.assertIsNone(caught.exception.__context__)
                self.assertNotIn("fixture-sensitive", str(caught.exception))
                self.get.assert_called_once()

    def test_http_temporary_statuses_are_retryable_and_close_response(self):
        for status in (408, 429, 500, 501, 502, 503, 504, 599):
            with self.subTest(status=status):
                self.get.reset_mock()
                self.response.close.reset_mock()
                self.response.status_code = status
                with self.assertRaises(live.LiveTransientError) as caught:
                    self.client().resolve()
                self.assertTrue(caught.exception.retryable)
                self.get.assert_called_once()
                self.response.close.assert_called_once()

    def test_explicit_business_temporary_statuses_are_retryable(self):
        for field, values in (("status", (408, 429, 500, 502, 503, 504, 599, "503")),
                              ("code", (408, 429, 500, 501, 503, "429"))):
            for value in values:
                with self.subTest(field=field, value=value):
                    self.get.reset_mock()
                    self.response.json.return_value = {"status": 200, "ok": False,
                                                       field: value, "message": "fixture-sensitive"}
                    with self.assertRaises(live.LiveTransientError) as caught:
                        self.client().resolve()
                    self.assertTrue(caught.exception.retryable)
                    self.assertNotIn("fixture-sensitive", str(caught.exception))
                    self.get.assert_called_once()

    def test_known_not_started_status_and_future_window_are_not_retryable(self):
        future = live_payload()
        future["data"]["liveStartTime"] = (NOW + 60) * 1000
        cases = ({"status": 501}, {"status": "501", "data": {}}, future)
        for body in cases:
            with self.subTest(body=body):
                self.get.reset_mock()
                self.response.json.return_value = body
                with self.assertRaisesRegex(live.LiveError, "尚未开始") as caught:
                    self.client().resolve()
                self.assertFalse(caught.exception.retryable)
                self.get.assert_called_once()

    def test_only_empty_device_lists_are_temporarily_unavailable(self):
        body = live_payload()
        body["data"].update(courseDeviceViewDtoList=[], continueCourId=456)
        self.response.json.return_value = body
        client = self.client()
        with self.assertRaises(live.LiveTransientError) as caught:
            client.resolve()
        self.assertTrue(caught.exception.retryable)
        self.get.assert_called_once()
        self.response.json.return_value = live_payload()
        self.assertIsInstance(client.resolve(), live.LiveStream)
        self.assertEqual(client.course_id, "123")
        self.assertEqual([call.kwargs["params"]["courseId"] for call in self.get.call_args_list],
                         ["123", "123"])
        for value in (None, {}, "", [None], [{}]):
            with self.subTest(value=value):
                self.get.reset_mock()
                self.response.json.return_value["data"]["courseDeviceViewDtoList"] = value
                with self.assertRaises(live.LiveError) as caught:
                    client.resolve()
                self.assertFalse(caught.exception.retryable)
                self.get.assert_called_once()

    def test_ended_class_is_terminal_and_never_follows_continuation_id(self):
        body = live_payload()
        body["data"].update(liveEndTime=NOW * 1000, continueCourId=456,
                            continueCourRefreshTime=(NOW + 60) * 1000)
        self.response.json.return_value = body
        client = self.client()
        with self.assertRaises(live.LiveEnded) as caught:
            client.resolve()
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(client.course_id, "123")
        self.get.assert_called_once()
        self.assertEqual(self.get.call_args.kwargs["params"]["courseId"], "123")

    def test_known_live_window_uses_server_time_and_stops_past_or_future_classes(self):
        for start, end, message in ((NOW + 1, NOW + 600, "尚未开始"),
                                    (NOW - 600, NOW, "已经结束"),
                                    (NOW, NOW - 1, "无法确认")):
            with self.subTest(message=message):
                body = live_payload()
                body["data"].update(liveStartTime=start * 1000, liveEndTime=end * 1000)
                self.response.json.return_value = body
                with self.assertRaisesRegex(live.LiveError, message) as caught:
                    self.client().resolve()
                self.assertFalse(caught.exception.retryable)
        self.response.json.return_value = live_payload()
        with patch.object(live.time, "time", return_value=NOW + 100_000):
            self.assertIsInstance(self.client().resolve(), live.LiveStream)

    def test_optional_id_and_time_fields_do_not_reject_an_explicit_live_flv_response(self):
        body = live_payload()
        for field in ("id", "liveStartTime", "liveEndTime"):
            body["data"].pop(field)
        self.response.json.return_value = body
        self.assertIsInstance(self.client().resolve(), live.LiveStream)

    def test_wrong_course_or_invalid_supplied_time_is_not_played(self):
        for fields, message in (({"id": 456}, "课程与链接不一致"),
                                 ({"liveEndTime": "invalid"}, "无法确认"),
                                 ({"liveStartTime": True}, "无法确认")):
            with self.subTest(fields=fields):
                body = live_payload()
                body["data"].update(fields)
                self.response.json.return_value = body
                with self.assertRaisesRegex(live.LiveError, message):
                    self.client().resolve()

    def test_unavailable_or_non_flv_streams_do_not_fall_back_to_hls_or_replay(self):
        for urls in ([""], ["https://video.hdu.edu.cn/a.m3u8"],
                     ["https://video.hdu.edu.cn/history.mp4"], ["file:///secret.flv"],
                     ["https://user:fixture-password@video.hdu.edu.cn/live.flv"],
                     ["https://video.hdu.edu.cn/a.flv\r\nX: value"]):
            with self.subTest(urls=urls):
                self.get.reset_mock()
                body = live_payload()
                body["data"]["courseDeviceViewDtoList"] = [
                    {"chanNameMainPlayUrl": url, "mainTokenStr": "fixture"} for url in urls]
                self.response.json.return_value = body
                with self.assertRaisesRegex(live.LiveError, "电脑声音模式") as caught:
                    self.client().resolve()
                self.assertFalse(caught.exception.retryable)
                self.get.assert_called_once()
                self.assertEqual(self.get.call_args.kwargs["params"]["playType"], 2)

    def test_network_and_parser_errors_never_echo_credentials_or_exception_context(self):
        for kind in ("network", "json", "server"):
            with self.subTest(kind=kind):
                self.get.side_effect = None
                self.response.json.side_effect = None
                self.response.json.return_value = live_payload()
                if kind == "network":
                    self.get.side_effect = requests.ConnectionError("fixture-secret-in-url")
                elif kind == "json":
                    self.response.json.side_effect = ValueError("fixture-secret-in-body")
                else:
                    self.response.json.return_value["data"]["errorMsg"] = "fixture-secret-from-server"
                try:
                    self.client().resolve()
                except live.LiveError as exc:
                    rendered = "".join(traceback.format_exception(exc))
                    self.assertNotIn("fixture-secret", rendered)
                    self.assertIsNone(exc.__context__)
                    self.assertEqual(exc.retryable, kind == "network")
                else:
                    self.fail("Expected a safe error")

    def test_malformed_business_responses_fail_without_retry(self):
        for body in ([], None, {"status": True}, {"status": 200, "data": []},
                     {"status": 200, "ok": False, "data": {}},
                     {"status": "503 invalid"}, {"status": 503.0},
                     {"status": 200, "ok": False, "message": "503 server unavailable"}):
            with self.subTest(body=body):
                self.get.reset_mock()
                self.response.json.return_value = body
                with self.assertRaises(live.LiveError) as caught:
                    self.client().resolve()
                self.assertFalse(caught.exception.retryable)
                self.get.assert_called_once()

    def test_close_erases_api_credentials_and_prevents_reuse(self):
        client = self.client()
        with client:
            client.resolve()
        client.close()
        self.assertNotIn("jwt-token", client._session.headers)
        self.assertFalse(list(client._session.cookies))
        with self.assertRaisesRegex(live.LiveError, "已关闭"):
            client.resolve()
        self.get.assert_called_once()


class LiveLoginTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.page = Mock()
        self.page.url = ORIGIN + "/#/list-live"
        self.page.is_closed.return_value = False
        self.page.evaluate.return_value = {"ok": True, "jwt_token": "fixture-jwt"}
        self.ticks = 0

        def tick(_):
            self.ticks += 1
            if self.ticks > 6:
                raise AssertionError("Mock browser exceeded its bounded login loop")
        self.page.wait_for_timeout.side_effect = tick
        self.context = Mock()
        self.context.pages = [self.page]
        self.context.cookies.return_value = [cookie(), cookie("unrelated", "skl.hdu.edu.cn")]
        self.profile_response = Mock()
        self.profile_response.status = 200
        self.profile_response.json.return_value = {"ok": True, "status": 200, "data": {"id": 1}}
        self.context.request.get.return_value = self.profile_response
        self.playwright = Mock()
        self.playwright.chromium.launch_persistent_context.return_value = self.context
        self.live_browser = Mock()
        self.live_browser.new_context.return_value = self.context
        self.playwright.chromium.launch.return_value = self.live_browser
        self.manager = MagicMock()
        self.manager.__enter__.return_value = self.playwright
        self.patches = [
            patch.object(live, "application_root", return_value=self.root),
            patch.object(live, "configure_browser_runtime"),
            patch.object(live, "sync_playwright", return_value=self.manager),
            patch.object(browser, "_browser_args", return_value=[]),
            patch.object(browser, "load_secrets",
                         return_value={"skl_username": "fixture-user", "skl_password": "fixture-password"}),
            patch.object(browser, "try_sso_login", return_value=True),
            patch.object(browser, "_cleanup_stale_profile",
                         side_effect=AssertionError("Must not touch attendance profile")),
            patch.object(requests.Session, "request",
                         side_effect=AssertionError("Network is forbidden in this test")),
        ]
        self.mocks = [item.start() for item in self.patches]
        for item in self.patches:
            self.addCleanup(item.stop)

    def run_login(self, stop=None, *, switch_account=False):
        output = StringIO()
        with redirect_stdout(output):
            result = live.login_live(PAGE, stop, switch_account=switch_account)
        return result, output.getvalue()

    def test_login_uses_independent_profile_and_saves_only_verified_origin(self):
        result, output = self.run_login()
        self.assertEqual(result, 0)
        self.assertIn("直播登录已保存", output)
        self.assertNotIn("fixture", output)
        args, kwargs = self.playwright.chromium.launch_persistent_context.call_args
        self.assertEqual(args[0], str(self.root / "live_profile"))
        self.assertFalse(kwargs["headless"])
        self.page.goto.assert_called_once_with(
            ORIGIN + "/#/list-live", wait_until="domcontentloaded", timeout=10000)
        saved = json.loads((self.root / "live_session.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["origin"], ORIGIN)
        self.assertEqual([c["name"] for c in saved["cookies"]], ["fixture-session"])
        self.assertEqual(saved["jwt_token"], "fixture-jwt")
        self.assertNotIn("login_mode", saved)
        self.assertEqual(self.page.evaluate.call_args.args[1],
                         {"origin": ORIGIN})
        self.assertIn("location.origin !== origin", self.page.evaluate.call_args.args[0])
        self.assertNotIn("fetch(", self.page.evaluate.call_args.args[0])
        self.context.request.get.assert_called_once_with(
            ORIGIN + live._API + "/v1/currentuser", headers={"Accept": "application/json", "jwt-token": "fixture-jwt"},
            timeout=4000, max_redirects=0, fail_on_status_code=False)
        self.assertFalse(list(self.root.glob(".live-session-*")))
        self.context.close.assert_called_once()
        self.playwright.chromium.launch.assert_not_called()
        browser.load_secrets.assert_not_called()
        browser._cleanup_stale_profile.assert_not_called()

    def test_entered_credentials_override_previous_manual_account_in_fresh_context(self):
        stored = self.root / "live_session.json"
        stored.write_text(json.dumps({"origin": ORIGIN, "login_mode": "manual"}), encoding="utf-8")
        credentials = {"skl_username": "new-student", "skl_password": "new-password"}
        self.page.url = "https://sso.hdu.edu.cn/login"
        self.page.wait_for_timeout.side_effect = lambda _: setattr(self.page, "url", ORIGIN + "/#/list-live")
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(live.login_live(PAGE, credentials=credentials), 0)
        self.playwright.chromium.launch_persistent_context.assert_not_called()
        self.live_browser.new_context.assert_called_once_with()
        browser.try_sso_login.assert_called_once_with(self.page, credentials)
        browser.load_secrets.assert_not_called()
        saved = json.loads(stored.read_text(encoding="utf-8"))
        self.assertEqual(saved["username"], "new-student")
        self.assertNotIn("login_mode", saved)
        self.assertNotIn("new-password", stored.read_text(encoding="utf-8") + output.getvalue())
        self.context.close.assert_called_once()
        self.live_browser.close.assert_called_once()

    def test_attendance_profiles_follow_the_entered_account(self):
        first = browser.account_profile({"skl_username": "student-a"})
        self.assertEqual(first, browser.account_profile({"skl_username": " student-a "}))
        self.assertNotEqual(first, browser.account_profile({"skl_username": "student-b"}))
        self.assertTrue(first.is_relative_to(browser.PROFILE))
        self.assertEqual(first, browser.account_profile({"skl_username": "student-a", "skl_password": "changed"}))

    def test_login_cancellation_before_launch_or_after_probe_does_not_save(self):
        stop = threading.Event()
        stop.set()
        result, output = self.run_login(stop)
        self.assertEqual(result, 1)
        self.assertIn("已取消", output)
        live.sync_playwright.assert_not_called()
        stop.clear()
        def verify(*_):
            stop.set()
            return {"ok": True, "jwt_token": "fixture-jwt"}
        self.page.evaluate.side_effect = verify
        result, output = self.run_login(stop)
        self.assertEqual(result, 1)
        self.assertIn("已取消", output)
        self.assertFalse((self.root / "live_session.json").exists())
        self.context.close.assert_called_once()

    def test_timeout_and_closed_window_report_reason_without_overwriting_session(self):
        stored = self.root / "live_session.json"
        stored.write_text("previous-fixture-session", encoding="utf-8")
        with patch.object(live, "_LOGIN_TIMEOUT_SECONDS", 0):
            result, output = self.run_login()
        self.assertEqual(result, 1)
        self.assertIn("超时", output)
        self.page.is_closed.return_value = True
        result, output = self.run_login()
        self.assertEqual(result, 1)
        self.assertIn("窗口已关闭", output)
        self.assertEqual(stored.read_text(encoding="utf-8"), "previous-fixture-session")
        self.assertEqual(self.context.close.call_count, 2)

    def test_known_sso_waits_and_retries_an_unready_form_before_success(self):
        self.page.url = "https://sso.hdu.edu.cn/login"
        browser.try_sso_login.side_effect = [False, True]
        def tick(_):
            self.ticks += 1
            if self.ticks == 2:
                self.page.url = ORIGIN + "/#/list-live"
        self.page.wait_for_timeout.side_effect = tick
        result, output = self.run_login()
        self.assertEqual(result, 0)
        self.assertEqual(browser.try_sso_login.call_count, 2)
        self.assertEqual(self.page.wait_for_selector.call_count, 2)
        self.assertNotIn("fixture-password", output)

    def test_unknown_sso_host_never_reads_or_fills_credentials(self):
        self.page.url = "https://sso.hdu.edu.cn.example.com/login"
        stop = threading.Event()
        self.page.wait_for_timeout.side_effect = lambda _: stop.set()
        result, _ = self.run_login(stop)
        self.assertEqual(result, 1)
        browser.load_secrets.assert_not_called()
        browser.try_sso_login.assert_not_called()
        self.page.evaluate.assert_not_called()

    def test_redirect_while_loading_secrets_is_rechecked_before_filling(self):
        self.page.url = "https://sso.hdu.edu.cn/login"
        def read_secrets():
            self.page.url = "https://example.com/login"
            return {"skl_username": "fixture", "skl_password": "fixture-password"}
        browser.load_secrets.side_effect = read_secrets
        stop = threading.Event()
        self.page.wait_for_timeout.side_effect = lambda _: stop.set()
        result, _ = self.run_login(stop)
        self.assertEqual(result, 1)
        browser.try_sso_login.assert_not_called()

    def test_failed_verification_never_saves_browser_credentials(self):
        self.page.evaluate.return_value = {"ok": False, "jwt_token": "fixture-invalid-jwt"}
        stop = threading.Event()
        self.page.wait_for_timeout.side_effect = lambda _: stop.set()
        result, _ = self.run_login(stop)
        self.assertEqual(result, 1)
        self.context.cookies.assert_not_called()
        self.assertFalse((self.root / "live_session.json").exists())

    def test_switch_uses_empty_context_and_only_saves_the_manual_new_login(self):
        stored = self.root / "live_session.json"
        stored.write_text("old-fixture-session", encoding="utf-8")
        profiles = []
        for name in ("live_profile", "browser_profile"):
            profile = self.root / name
            profile.mkdir()
            marker = profile / "previous-login"
            marker.write_text("old-fixture-profile", encoding="utf-8")
            profiles.append(marker)
        self.context.pages = []
        self.context.new_page.return_value = self.page
        self.page.url = "https://sso.hdu.edu.cn/login"
        self.page.evaluate.return_value = {"ok": True, "jwt_token": "fixture-new-jwt"}
        self.context.cookies.return_value = [cookie(value="fixture-new-cookie")]

        def manual_login(_):
            self.assertEqual(stored.read_text(encoding="utf-8"), "old-fixture-session")
            self.page.evaluate.assert_not_called()
            browser.load_secrets.assert_not_called()
            browser.try_sso_login.assert_not_called()
            self.page.url = ORIGIN + "/#/list-live"
        self.page.wait_for_timeout.side_effect = manual_login

        result, output = self.run_login(switch_account=True)
        self.assertEqual(result, 0)
        self.assertIn("手动登录", output)
        self.assertIn("直播登录已保存", output)
        self.assertNotIn("fixture", output)
        self.playwright.chromium.launch.assert_called_once_with(
            headless=False, args=[], timeout=30000)
        self.playwright.chromium.launch_persistent_context.assert_not_called()
        self.live_browser.new_context.assert_called_once_with()
        self.context.add_cookies.assert_not_called()
        self.context.add_init_script.assert_not_called()
        self.context.new_page.assert_called_once_with()
        self.context.route.assert_called_once_with("**/*", live._block_viewing_writes)
        self.page.goto.assert_called_once_with(
            ORIGIN + "/#/list-live", wait_until="domcontentloaded", timeout=10000)
        self.page.wait_for_selector.assert_not_called()
        browser.load_secrets.assert_not_called()
        browser.try_sso_login.assert_not_called()
        browser._cleanup_stale_profile.assert_not_called()
        saved = json.loads(stored.read_text(encoding="utf-8"))
        self.assertEqual(saved["jwt_token"], "fixture-new-jwt")
        self.assertEqual(saved["cookies"][0]["value"], "fixture-new-cookie")
        self.assertEqual(saved["login_mode"], "manual")
        for marker in profiles:
            self.assertEqual(marker.read_text(encoding="utf-8"), "old-fixture-profile")
        self.context.close.assert_called_once()
        self.live_browser.close.assert_called_once()

    def test_normal_login_after_manual_switch_never_reuses_the_old_profile(self):
        result, _ = self.run_login(switch_account=True)
        self.assertEqual(result, 0)
        stored = self.root / "live_session.json"
        previous = stored.read_bytes()
        self.page.url = "https://sso.hdu.edu.cn/login"
        self.page.evaluate.return_value = {"ok": True, "jwt_token": "fixture-renewed-jwt"}

        def manual_login(_):
            self.assertEqual(stored.read_bytes(), previous)
            browser.load_secrets.assert_not_called()
            browser.try_sso_login.assert_not_called()
            self.page.url = ORIGIN + "/#/list-live"
        self.page.wait_for_timeout.side_effect = manual_login

        result, output = self.run_login()
        self.assertEqual(result, 0)
        self.assertIn("手动登录", output)
        self.playwright.chromium.launch_persistent_context.assert_not_called()
        self.assertEqual(self.playwright.chromium.launch.call_count, 2)
        self.assertEqual(self.live_browser.new_context.call_count, 2)
        browser.load_secrets.assert_not_called()
        browser.try_sso_login.assert_not_called()
        browser._cleanup_stale_profile.assert_not_called()
        saved = json.loads(stored.read_text(encoding="utf-8"))
        self.assertEqual(saved["jwt_token"], "fixture-renewed-jwt")
        self.assertEqual(saved["login_mode"], "manual")
        self.assertEqual(self.context.close.call_count, 2)
        self.assertEqual(self.live_browser.close.call_count, 2)

    def test_only_a_same_origin_manual_flag_changes_the_default_login(self):
        stored = self.root / "live_session.json"
        for saved in ({"origin": ORIGIN}, {"origin": VPN, "login_mode": "manual"},
                      {"login_mode": "manual"}, {"origin": ORIGIN, "login_mode": "other"},
                      {"origin": ORIGIN, "login_mode": True}, []):
            with self.subTest(saved=saved):
                stored.write_text(json.dumps(saved), encoding="utf-8")
                self.playwright.chromium.launch_persistent_context.reset_mock()
                result, _ = self.run_login()
                self.assertEqual(result, 0)
                self.playwright.chromium.launch_persistent_context.assert_called_once()
                self.playwright.chromium.launch.assert_not_called()
                self.assertNotIn("login_mode", json.loads(stored.read_text(encoding="utf-8")))

    def test_remembered_manual_mode_preserves_session_on_cancel_or_save_failure(self):
        stored = self.root / "live_session.json"
        original = json.dumps({"origin": ORIGIN, "login_mode": "manual",
                               "jwt_token": "fixture-old-jwt", "cookies": [cookie()]})
        stored.write_text(original, encoding="utf-8")
        stop = threading.Event()

        def verify(*_):
            stop.set()
            return {"ok": True, "jwt_token": "fixture-new-jwt"}
        self.page.evaluate.side_effect = verify
        result, output = self.run_login(stop)
        self.assertEqual(result, 1)
        self.assertIn("已取消", output)
        self.assertEqual(stored.read_text(encoding="utf-8"), original)
        self.page.evaluate.side_effect = None
        with patch.object(live.os, "replace", side_effect=PermissionError("fixture-private-error")):
            with self.assertRaises(live.LiveError):
                self.run_login()
        self.assertEqual(stored.read_text(encoding="utf-8"), original)
        self.assertFalse(list(self.root.glob(".live-session-*")))
        self.playwright.chromium.launch_persistent_context.assert_not_called()
        browser.load_secrets.assert_not_called()
        self.assertEqual(self.context.close.call_count, 2)
        self.assertEqual(self.live_browser.close.call_count, 2)

    def test_switch_failed_verification_preserves_previous_session(self):
        stored = self.root / "live_session.json"
        stored.write_text("old-fixture-session", encoding="utf-8")
        self.page.evaluate.return_value = {"ok": False, "jwt_token": "fixture-invalid-jwt"}
        stop = threading.Event()
        self.page.wait_for_timeout.side_effect = lambda _: stop.set()
        result, output = self.run_login(stop, switch_account=True)
        self.assertEqual(result, 1)
        self.assertIn("已取消", output)
        self.assertEqual(stored.read_text(encoding="utf-8"), "old-fixture-session")
        self.context.cookies.assert_not_called()
        self.context.close.assert_called_once()
        self.live_browser.close.assert_called_once()
        browser.load_secrets.assert_not_called()

    def test_switch_cancellation_before_launch_preserves_previous_session(self):
        stored = self.root / "live_session.json"
        stored.write_text("old-fixture-session", encoding="utf-8")
        stop = threading.Event()
        stop.set()
        result, output = self.run_login(stop, switch_account=True)
        self.assertEqual(result, 1)
        self.assertIn("已取消", output)
        live.sync_playwright.assert_not_called()
        self.playwright.chromium.launch.assert_not_called()
        self.assertEqual(stored.read_text(encoding="utf-8"), "old-fixture-session")

    def test_switch_cancellation_during_verification_or_cookie_capture_does_not_save(self):
        stored = self.root / "live_session.json"
        stored.write_text("old-fixture-session", encoding="utf-8")
        for phase in ("probe", "cookies"):
            with self.subTest(phase=phase):
                stop = threading.Event()
                self.context.cookies.reset_mock()
                self.context.close.reset_mock()
                self.live_browser.close.reset_mock()

                def verify(*_):
                    if phase == "probe":
                        stop.set()
                    return {"ok": True, "jwt_token": "fixture-new-jwt"}

                def get_cookies(*_):
                    stop.set()
                    return [cookie(value="fixture-new-cookie")]

                self.page.evaluate.side_effect = verify
                self.context.cookies.side_effect = get_cookies
                result, output = self.run_login(stop, switch_account=True)
                self.assertEqual(result, 1)
                self.assertIn("已取消", output)
                self.assertEqual(stored.read_text(encoding="utf-8"), "old-fixture-session")
                if phase == "probe":
                    self.context.cookies.assert_not_called()
                self.context.close.assert_called_once()
                self.live_browser.close.assert_called_once()

    def test_switch_timeout_and_closed_window_preserve_session_and_close_browser(self):
        stored = self.root / "live_session.json"
        stored.write_text("old-fixture-session", encoding="utf-8")
        with patch.object(live, "_LOGIN_TIMEOUT_SECONDS", 0):
            result, output = self.run_login(switch_account=True)
        self.assertEqual(result, 1)
        self.assertIn("超时", output)
        self.page.is_closed.return_value = True
        result, output = self.run_login(switch_account=True)
        self.assertEqual(result, 1)
        self.assertIn("窗口已关闭", output)
        self.assertEqual(stored.read_text(encoding="utf-8"), "old-fixture-session")
        self.assertEqual(self.context.close.call_count, 2)
        self.assertEqual(self.live_browser.close.call_count, 2)

    def test_switch_context_creation_failure_closes_browser_and_preserves_session(self):
        stored = self.root / "live_session.json"
        stored.write_text("old-fixture-session", encoding="utf-8")
        self.live_browser.new_context.side_effect = live.PlaywrightError("fixture-private-error")
        with self.assertRaises(live.LiveError) as caught:
            self.run_login(switch_account=True)
        self.assertNotIn("fixture-private-error", str(caught.exception))
        self.assertIsNone(caught.exception.__context__)
        self.assertEqual(stored.read_text(encoding="utf-8"), "old-fixture-session")
        self.context.close.assert_not_called()
        self.live_browser.close.assert_called_once()

    def test_switch_save_failure_preserves_session_and_closes_all_resources(self):
        stored = self.root / "live_session.json"
        stored.write_text("old-fixture-session", encoding="utf-8")
        with patch.object(live.os, "replace", side_effect=PermissionError("fixture-private-error")):
            with self.assertRaises(live.LiveError):
                self.run_login(switch_account=True)
        self.assertEqual(stored.read_text(encoding="utf-8"), "old-fixture-session")
        self.assertFalse(list(self.root.glob(".live-session-*")))
        self.context.close.assert_called_once()
        self.live_browser.close.assert_called_once()

    def test_switch_closes_browser_even_if_context_close_fails(self):
        self.context.close.side_effect = live.PlaywrightError("fixture-private-error")
        result, output = self.run_login(switch_account=True)
        self.assertEqual(result, 0)
        self.assertIn("直播登录已保存", output)
        self.assertNotIn("fixture", output)
        self.context.close.assert_called_once()
        self.live_browser.close.assert_called_once()

    def test_atomic_save_failure_keeps_old_login_and_removes_temporary_file(self):
        stored = self.root / "live_session.json"
        stored.write_text("old-fixture", encoding="utf-8")
        with patch.object(live.os, "replace", side_effect=PermissionError("fixture-sensitive-value")):
            with self.assertRaises(live.LiveError) as caught:
                live._save_session(ORIGIN, [cookie()], "fixture-jwt")
        self.assertNotIn("fixture-sensitive-value", str(caught.exception))
        self.assertEqual(stored.read_text(encoding="utf-8"), "old-fixture")
        self.assertFalse(list(self.root.glob(".live-session-*")))

    def test_playback_writes_are_blocked_but_login_and_read_only_requests_continue(self):
        cases = [("POST", ORIGIN + live._API + "/v1/live/keepAlive", True),
                 ("POST", ORIGIN + live._API + "/v1/live/addPlayTimes", True),
                 ("POST", ORIGIN + live._API + "/v1/vod/addPlayTimes", True),
                 ("POST", ORIGIN + live._API + "/v1/data/reporting", True),
                 ("POST", ORIGIN + live._API + "/v1/statistics/visitRecord/create", True),
                 ("GET", ORIGIN + live._API + "/v1/currentuser", False),
                 ("GET", ORIGIN + live._API + "/v1/course_vod_videoinfos", False),
                 ("POST", "https://sso.hdu.edu.cn/login", False),
                 ("POST", ORIGIN + live._API + "/v1/web/login/acknowledge", False)]
        for method, url, blocked in cases:
            with self.subTest(method=method, url=url):
                route = Mock()
                route.request.method = method
                route.request.url = url
                live._block_viewing_writes(route)
                self.assertEqual(route.abort.called, blocked)
                self.assertEqual(route.continue_.called, not blocked)


if __name__ == "__main__":
    unittest.main()
