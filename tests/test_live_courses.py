"""Read-only live-course discovery contracts using fake HTTP responses only."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import requests

from hdusign import live


ORIGIN = "https://course.hdu.edu.cn"
VPN = "https://https-course-hdu-edu-cn-443.webvpn.hdu.edu.cn"
NOW = dt.datetime(
    2026, 9, 22, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=8))).timestamp()


def cookie(domain="course.hdu.edu.cn"):
    return {"name": "fixture-session", "value": "fixture-cookie-value",
            "domain": domain, "path": "/", "secure": True, "expires": -1}


def record(course_id=123, **changes):
    value = {
        "id": course_id,
        "teclId": 456,
        "courName": "  网络安全  ",
        "subjName": "课程名称后备值",
        "teacNames": [" 张老师 ", "", None, "李老师"],
        "clroName": " 第7教研楼219 ",
        "courBeginTime": "2026-09-22 08:55:00",
        "courEndTime": "2026-09-22 09:40:00",
        "letiNumber": 2,
    }
    value.update(changes)
    return value


def payload(records=None):
    return {"status": 200, "code": 200, "ok": True,
            "timestamp": 1_790_029_800_000,
            "data": {"records": [record()] if records is None else records}}


class LiveCourseUrlTests(unittest.TestCase):
    def test_url_uses_course_origin_and_normalised_numeric_ids(self):
        course = live.LiveCourse("00123", "课程", 1, 2, tecl_id="00456", origin=VPN)
        url = live.live_course_url(course)
        parsed = urlsplit(url)
        query = parse_qs(urlsplit(parsed.fragment).query)
        self.assertEqual(parsed.scheme + "://" + parsed.netloc, VPN)
        self.assertEqual(urlsplit(parsed.fragment).path, "/play-center")
        self.assertEqual(query, {"courseId": ["123"], "liveId": ["123"],
                                 "teclId": ["456"], "target": ["live"]})
        self.assertEqual(live.parse_live_page(url), (VPN, "123"))

    def test_url_accepts_an_absent_teaching_class_id(self):
        course = live.LiveCourse("123", "课程", 1, 2, origin=ORIGIN)
        query = parse_qs(urlsplit(urlsplit(live.live_course_url(course)).fragment).query)
        self.assertNotIn("teclId", query)

    def test_url_rejects_untrusted_origins_and_invalid_ids(self):
        cases = (
            live.LiveCourse("0", "课程", 1, 2),
            live.LiveCourse("abc", "课程", 1, 2),
            live.LiveCourse("123", "课程", 1, 2, tecl_id="-1"),
            live.LiveCourse("123", "课程", 1, 2, origin="https://example.com"),
            live.LiveCourse("123", "课程", 1, 2, origin=ORIGIN + "/path"),
        )
        for course in cases:
            with self.subTest(course=course):
                with self.assertRaisesRegex(live.LiveError, "资料无效"):
                    live.live_course_url(course)


class LiveCourseListTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.root_patch = patch.object(live, "application_root", return_value=self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.now_patch = patch.object(live, "_now", return_value=NOW)
        self.now_patch.start()
        self.addCleanup(self.now_patch.stop)
        self.request_patch = patch.object(
            requests.Session, "request",
            side_effect=AssertionError("Network is forbidden in this test"))
        self.request_patch.start()
        self.addCleanup(self.request_patch.stop)
        self.response = Mock()
        self.response.status_code = 200
        self.response.json.return_value = payload()
        self.get_patch = patch.object(
            requests.Session, "get", autospec=True, return_value=self.response)
        self.get = self.get_patch.start()
        self.addCleanup(self.get_patch.stop)
        self.save_auth()

    def save_auth(self, **changes):
        auth = {"version": 1, "origin": ORIGIN, "cookies": [cookie()],
                "jwt_token": "fixture-jwt"}
        auth.update(changes)
        (self.root / "live_session.json").write_text(
            json.dumps(auth), encoding="utf-8")

    def list(self, config=None):
        value = {"browser": {"bypass_proxy": True}} if config is None else config
        return live.list_live_courses(value)

    def test_authenticated_get_uses_saved_origin_and_exact_read_only_query(self):
        courses = self.list()
        self.assertEqual(len(courses), 1)
        args, kwargs = self.get.call_args
        session = args[0]
        self.assertEqual(args[1], ORIGIN + live._API + "/v1/vod_live/t-1")
        self.assertEqual(kwargs["params"], {
            "page.pageIndex": 1,
            "page.pageSize": 1000,
            "page.orders[0].asc": "true",
            "page.orders[0].field": "courBeginTime",
            "liveDay": 0,
        })
        self.assertEqual(kwargs["timeout"], (10, 15))
        self.assertFalse(kwargs["allow_redirects"])
        self.assertFalse(session.trust_env)
        self.assertEqual(session.headers.get("Referer"), ORIGIN + "/")
        self.assertNotIn("jwt-token", session.headers)
        self.assertFalse(list(session.cookies))
        self.response.close.assert_called_once()

    def test_records_are_normalised_deduplicated_and_sorted(self):
        earlier = record(
            7, courName="", subjName=" 密码学 ", teacNames=None, tecName=" 王老师 ",
            courBeginTime="2026-09-22T07:10:00+08:00",
            courEndTime="2026-09-22T07:55:00+08:00", letiNumber="3.0",
            teclId=None)
        duplicate = record(7, courName="重复记录",
                           courBeginTime="2026-09-22T07:10:00+08:00",
                           courEndTime="2026-09-22T07:55:00+08:00",
                           teclId=None, letiNumber="3.0")
        later = record(123)
        self.response.json.return_value = payload([later, earlier, duplicate])

        courses = self.list()

        self.assertEqual([course.course_id for course in courses], ["7", "123"])
        first, second = courses
        self.assertEqual((first.title, first.teacher, first.section, first.tecl_id),
                         ("重复记录", "张老师、李老师", "第3节", ""))
        self.assertEqual((second.title, second.teacher, second.classroom, second.section,
                          second.tecl_id, second.origin),
                         ("网络安全", "张老师、李老师", "第7教研楼219", "第2节",
                          "456", ORIGIN))
        expected_start = dt.datetime(
            2026, 9, 22, 8, 55, tzinfo=dt.timezone(dt.timedelta(hours=8))).timestamp()
        self.assertEqual(second.start, expected_start)

    def test_real_timetable_fields_create_one_three_lesson_course(self):
        rows = [record(123+i, subjId=67, clroId=89, acteId=9, letiNumber=3+i,
                       courBeginTime=f"2026-09-22 {hour}", courEndTime=f"2026-09-22 {end}")
                for i, (hour, end) in enumerate([
                    ("10:00:00", "10:45:00"), ("10:50:00", "11:35:00"),
                    ("11:40:00", "12:25:00")])]
        self.response.json.return_value = payload(rows)
        courses = self.list()
        self.assertEqual(len(courses), 1)
        self.assertEqual(courses[0].lesson_ids, ("123", "124", "125"))
        self.assertEqual(courses[0].end - courses[0].start, 145 * 60)
        self.assertEqual(courses[0].section, "第3–5节 · 3节连堂")

    def test_real_row_count_pagination_does_not_lose_the_third_lesson(self):
        pages = []
        for i, (start, end) in enumerate([("10:00:00", "10:45:00"),
                                         ("10:50:00", "11:35:00"),
                                         ("11:40:00", "12:25:00")]):
            body = payload([record(123+i, subjId=67, clroId=89, acteId=9, letiNumber=3+i,
                                   courBeginTime=f"2026-09-22 {start}",
                                   courEndTime=f"2026-09-22 {end}")])
            body['data'].update(rowCount=3, pageSize=1, pageCount=3, pageIndex=i+1)
            pages.append(body)
        self.response.json.side_effect = pages
        courses = self.list()
        self.assertEqual(courses[0].lesson_ids, ('123', '124', '125'))
        self.assertEqual(self.get.call_count, 3)

    def test_complete_own_timetable_recovers_an_already_hidden_first_cell(self):
        rows = [record(123+i, subjId=67, clroId=89, acteId=9, letiNumber=3+i,
                       courBeginTime=f"2026-09-22 {start}", courEndTime=f"2026-09-22 {end}")
                for i, (start, end) in enumerate([('10:00:00', '10:45:00'),
                                                  ('10:50:00', '11:35:00'),
                                                  ('11:40:00', '12:25:00')])]
        self.response.json.side_effect = [payload(rows[1:]), payload(rows)]
        courses = self.list({'live_audio': {'continuous': True}})
        self.assertEqual(len(courses), 1)
        self.assertEqual(courses[0].course_id, '123')
        self.assertEqual(courses[0].lesson_ids, ('123', '124', '125'))
        args = self.get.call_args_list[1]
        self.assertEqual(args.args[1], ORIGIN + live._API + '/v1/myself/curriculum')
        self.assertEqual(args.kwargs['params']['acteId'], '9')
        self.assertEqual(args.kwargs['params']['courBeginTime'], '2026-09-22 00:00:00')
        self.assertEqual(args.kwargs['params']['courEndTime'], '2026-09-23 23:59:59')
        self.assertNotIn('liveDay', args.kwargs['params'])

    def test_missing_timetable_and_mismatched_identity_keep_single_cells(self):
        rows = [record(123+i, subjId=67, clroId=89, acteId=9, letiNumber=3+i,
                       courBeginTime=f"2026-09-22 {start}", courEndTime=f"2026-09-22 {end}")
                for i, (start, end) in enumerate([('10:00:00', '10:45:00'),
                                                  ('10:50:00', '11:35:00')])]
        for table in (payload([]), {'status': 503}, payload([dict(rows[0], clroId=999), rows[1]])):
            with self.subTest(table=table):
                self.response.json.side_effect = [payload(rows), table]
                courses = self.list({'live_audio': {'continuous': True}})
                self.assertEqual(len(courses), 2)
                self.assertTrue(all(not course.lesson_ids for course in courses))

    def test_malformed_records_are_ignored_without_exposing_server_text(self):
        invalid = [
            None,
            [],
            record(0),
            record(True),
            record("abc"),
            record(2, courName="", subjName=""),
            record(3, courBeginTime="invalid"),
            record(4, courEndTime="2026-09-22 08:00:00"),
        ]
        valid = record(5, courName="", subjName=" 数据库 ", teacNames=["A", 3, "B\r\n"])
        self.response.json.return_value = payload(invalid + [valid])

        courses = self.list()

        self.assertEqual(len(courses), 1)
        self.assertEqual((courses[0].course_id, courses[0].title, courses[0].teacher),
                         ("5", "数据库", "A"))

    def test_vpn_session_is_used_for_vpn_config_and_cookie_domain(self):
        self.save_auth(origin=VPN, cookies=[cookie(".webvpn.hdu.edu.cn")])
        courses = self.list({"live_url": VPN + "/#/home",
                             "browser": {"bypass_proxy": True}})
        self.assertEqual(courses[0].origin, VPN)
        self.assertTrue(self.get.call_args.args[1].startswith(VPN + live._API))

    def test_config_origin_must_match_the_saved_authenticated_origin(self):
        self.save_auth(origin=VPN, cookies=[cookie(".webvpn.hdu.edu.cn")])
        with self.assertRaisesRegex(live.LiveLoginRequired, "登录并读取课程"):
            self.list({"live_url": ORIGIN + "/#/home",
                       "browser": {"bypass_proxy": True}})
        self.get.assert_not_called()

    def test_missing_or_invalid_auth_never_accesses_the_network(self):
        stored = self.root / "live_session.json"
        cases = (
            None,
            "not-json",
            json.dumps([]),
            json.dumps({"origin": "https://example.com", "cookies": [cookie()]}),
            json.dumps({"origin": ORIGIN, "cookies": [], "jwt_token": ""}),
            json.dumps({"origin": ORIGIN, "cookies": [], "jwt_token": "bad\rjwt"}),
        )
        for saved in cases:
            with self.subTest(saved=saved):
                self.get.reset_mock()
                stored.unlink(missing_ok=True)
                if saved is not None:
                    stored.write_text(saved, encoding="utf-8")
                with self.assertRaises(live.LiveLoginRequired):
                    self.list()
                self.get.assert_not_called()

    def test_http_and_business_auth_or_permission_failures_are_specific(self):
        cases = (
            (302, payload(), live.LiveLoginRequired, "登录"),
            (401, payload(), live.LiveLoginRequired, "登录"),
            (403, payload(), live.LiveError, "无权"),
            (200, {"status": 401}, live.LiveLoginRequired, "登录"),
            (200, {"status": 403}, live.LiveError, "无权"),
            (503, payload(), live.LiveTransientError, "稍后重试"),
            (200, {"status": 503}, live.LiveTransientError, "稍后重试"),
        )
        for status, body, error, message in cases:
            with self.subTest(status=status, body=body):
                self.get.reset_mock()
                self.response.reset_mock()
                self.response.status_code = status
                self.response.json.return_value = body
                with self.assertRaisesRegex(error, message) as caught:
                    self.list()
                self.assertEqual(caught.exception.retryable,
                                 error is live.LiveTransientError)
                self.get.assert_called_once()
                self.assertGreaterEqual(self.response.close.call_count, 1)

    def test_malformed_success_responses_are_rejected(self):
        bodies = (
            [],
            None,
            {"status": True, "data": {"records": []}},
            {"status": 200, "ok": False, "data": {"records": []}},
            {"status": 200, "ok": True, "data": []},
            {"status": 200, "ok": True, "data": {"records": {}}},
        )
        for body in bodies:
            with self.subTest(body=body):
                self.get.reset_mock()
                self.response.reset_mock()
                self.response.status_code = 200
                self.response.json.side_effect = None
                self.response.json.return_value = body
                with self.assertRaises(live.LiveError) as caught:
                    self.list()
                self.assertFalse(caught.exception.retryable)
                self.assertNotIn("fixture", str(caught.exception))
                self.assertGreaterEqual(self.response.close.call_count, 1)

    def test_transport_errors_are_safe_and_classified(self):
        cases = (
            (requests.ConnectTimeout("fixture-private"), live.LiveTransientError),
            (requests.ConnectionError("fixture-private"), live.LiveTransientError),
            (requests.exceptions.SSLError("fixture-private"), live.LiveError),
            (requests.exceptions.InvalidURL("fixture-private"), live.LiveError),
        )
        for failure, error in cases:
            with self.subTest(failure=type(failure).__name__):
                self.get.reset_mock()
                self.get.side_effect = failure
                with self.assertRaises(error) as caught:
                    self.list()
                self.assertEqual(caught.exception.retryable,
                                 error is live.LiveTransientError)
                self.assertNotIn("fixture-private", str(caught.exception))
        self.get.side_effect = None

    def test_pagination_includes_all_records_when_server_caps_the_page_size(self):
        first = payload([record(1)])
        first["data"]["total"] = 2
        second = payload([record(2)])
        second["data"]["total"] = 2
        self.response.json.side_effect = [first, second]
        self.assertEqual([course.course_id for course in self.list()], ["1", "2"])
        self.assertEqual([call.kwargs["params"]["page.pageIndex"] for call in self.get.call_args_list], [1, 2])
        self.assertEqual(self.response.close.call_count, 2)

    def test_one_home_query_is_filtered_to_today_and_tomorrow_locally(self):
        body = payload([
            record(1, courBeginTime="2026-09-20 08:00:00",
                   courEndTime="2026-09-20 08:45:00"),
            record(2),
            record(3, courBeginTime="2026-09-23 10:00:00",
                   courEndTime="2026-09-23 11:35:00"),
            record(4, courBeginTime="2026-09-24 10:00:00",
                   courEndTime="2026-09-24 11:35:00"),
        ])
        self.response.json.return_value = body

        courses = self.list()

        self.assertEqual([course.course_id for course in courses], ["2", "3"])
        self.assertEqual([call.kwargs["params"]["liveDay"] for call in self.get.call_args_list], [0])
        self.assertEqual([call.kwargs["params"]["page.pageIndex"] for call in self.get.call_args_list], [1])

    def test_date_filter_uses_beijing_time_at_the_start_of_the_request(self):
        with patch.object(live, "_now", return_value=NOW + 12 * 60 * 60):
            self.response.json.return_value = payload([
                record(1, courBeginTime="2026-09-23 00:30:00",
                       courEndTime="2026-09-23 01:15:00"),
                record(2, courBeginTime="2026-09-24 08:00:00",
                       courEndTime="2026-09-24 08:45:00"),
            ])
            courses = self.list()
        self.assertEqual([course.course_id for course in courses], ["1", "2"])

    def test_pagination_is_deduplicated_and_filtered_after_all_pages(self):
        first = payload([
            record(2, courBeginTime="2026-09-23 10:00:00",
                   courEndTime="2026-09-23 11:35:00"),
            record(5, courBeginTime="2026-09-25 10:00:00",
                   courEndTime="2026-09-25 11:35:00"),
        ])
        first["data"]["total"] = 3
        second = payload([
            record(1, courBeginTime="2026-09-22 07:10:00",
                   courEndTime="2026-09-22 07:55:00"),
            record(2, courBeginTime="2026-09-23 10:00:00",
                   courEndTime="2026-09-23 11:35:00"),
        ])
        second["data"]["total"] = 3
        self.response.json.side_effect = [first, second]

        courses = self.list()

        self.assertEqual([course.course_id for course in courses], ["1", "2"])
        self.assertEqual([call.kwargs["params"]["page.pageIndex"] for call in self.get.call_args_list], [1, 2])

    def test_repeated_page_fails_instead_of_returning_an_incomplete_list(self):
        body = payload()
        body["data"]["total"] = 3
        self.response.json.return_value = body
        with self.assertRaisesRegex(live.LiveError, "分页未更新"):
            self.list()
        self.assertEqual(self.get.call_count, 2)

    def test_cancellation_discards_results_and_does_not_fetch_another_page(self):
        stop = threading.Event()
        def response_body():
            stop.set()
            return payload()
        self.response.json.side_effect = response_body
        with self.assertRaisesRegex(live.LiveError, "已取消"):
            live.list_live_courses({}, stop=stop)
        self.get.assert_called_once()
        self.get.reset_mock()
        with self.assertRaisesRegex(live.LiveError, "已取消"):
            live.list_live_courses({}, stop=stop)
        self.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
