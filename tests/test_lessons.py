"""Real timetable-shaped fixtures; no school requests, recording or sign-in."""
from contextlib import ExitStack
from dataclasses import asdict, replace
import datetime as dt
import threading
import unittest
from unittest.mock import patch

from hdusign import live, monitor
from hdusign.lessons import DeadlineStop, LessonPlan, group_lessons
from hdusign.media import MediaError
import test_live_monitor as fixtures

BASE = dt.datetime(2026, 10, 20, 10, tzinfo=dt.timezone(dt.timedelta(hours=8))).timestamp()


def lessons(count=3):
    return [live.LiveCourse(str(123 + i), "测试课", BASE + i * 3000, BASE + i * 3000 + 2700,
                            tecl_id="45", subject_id="67", room_id="89", term_id="9",
                            number=3 + i) for i in range(count)]


class LessonPlanTests(unittest.TestCase):
    def test_two_and_three_cells_merge_with_the_full_class_duration(self):
        for count in (2, 3):
            with self.subTest(count=count):
                courses = group_lessons(lessons(count))
                self.assertEqual(len(courses), 1)
                course = courses[0]
                self.assertEqual(course.lesson_ids, tuple(str(123 + i) for i in range(count)))
                self.assertEqual((course.start, course.end), (BASE, lessons(count)[-1].end))
                self.assertIn(f"{count}节连堂", course.section)

    def test_second_lesson_keeps_first_id_and_third_uses_first_plus_two(self):
        course = group_lessons(lessons())[0]
        plan = LessonPlan.from_mapping(asdict(course), "123")
        self.assertEqual(plan.window(BASE + 3200), ("123", BASE + 5880))
        self.assertEqual(plan.window(BASE + 5879.99), ("123", BASE + 5880))
        self.assertEqual(plan.window(BASE + 5880), ("125", BASE + 8820))
        self.assertIsNone(plan.window(BASE + 8820))

    def test_two_lessons_never_increment_the_stream_id(self):
        plan = LessonPlan.from_mapping(asdict(group_lessons(lessons(2))[0]), "124")
        self.assertEqual(plan.window(BASE + 5699), ("123", BASE + 5820))
        self.assertIsNone(plan.window(BASE + 5820))

    def test_every_break_resumes_two_minutes_before_next_cell(self):
        for count in (2, 3):
            with self.subTest(count=count):
                plan = LessonPlan.from_mapping(asdict(group_lessons(lessons(count))[0]), "123")
                for index in range(1, count):
                    end = BASE + (index - 1) * 3000 + 2700
                    wake = BASE + index * 3000 - 120
                    self.assertEqual(plan.break_until(end), wake)
                    self.assertEqual(plan.break_until(wake - 0.01), wake)
                    self.assertIsNone(plan.break_until(wake))
                    self.assertEqual(plan.upcoming_start(wake), BASE + index * 3000)

    def test_different_identity_day_lunch_or_missing_cell_does_not_merge(self):
        first, second = lessons(2)
        variants = [replace(second, tecl_id="46"), replace(second, subject_id="68"),
                    replace(second, room_id="90"), replace(second, term_id="10"),
                    replace(second, number=5), replace(second, subject_id=""),
                    replace(second, start=second.start + 86400, end=second.end + 86400),
                    replace(second, start=second.start + 7200, end=second.end + 7200)]
        for other in variants:
            with self.subTest(other=other):
                self.assertEqual(len(group_lessons([first, other])), 2)

    def test_unsupported_id_sequence_does_not_silently_group_the_tail(self):
        rows = lessons()
        rows[1] = replace(rows[1], course_id="1240")
        rows[2] = replace(rows[2], course_id="1241")
        self.assertEqual(len(group_lessons(rows)), 3)
        self.assertEqual(len(group_lessons(lessons(4))), 4)

    def test_plan_survives_saved_schedule_json_lists(self):
        value = asdict(group_lessons(lessons())[0])
        for key in ("lesson_ids", "lesson_starts", "lesson_ends"):
            value[key] = list(value[key])
        self.assertEqual(LessonPlan.from_mapping(value, "123").ids, ("123", "124", "125"))

    def test_unverified_or_malformed_plans_fail_before_media_starts(self):
        original = asdict(group_lessons(lessons())[0])
        changes = [dict(lesson_ids=["123", "124", "126"]),
                   dict(lesson_ids=["123", "123", "125"]),
                   dict(lesson_starts=[BASE]), dict(lesson_starts=[True, BASE+3000, BASE+6000]),
                   dict(lesson_ends=[float("inf"), BASE+5700, BASE+8700])]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                LessonPlan.from_mapping(dict(original, **change), "123")
        with self.assertRaises(ValueError):
            LessonPlan.from_mapping(original, "999")

    def test_deadline_does_not_cancel_parent_and_user_stop_is_immediate(self):
        clock = fixtures.VirtualClock()
        parent = fixtures.VirtualStop(clock)
        deadline = DeadlineStop(parent, 5, clock)
        self.assertTrue(deadline.wait(50))
        self.assertEqual(parent.waits, [5])
        self.assertFalse(parent.is_set())
        deadline = DeadlineStop(parent, 100, clock)
        deadline.set()
        self.assertTrue(parent.is_set())


class ContinuousMonitorTests(unittest.TestCase):
    setUp = fixtures.LiveMonitorTests.setUp
    successful_frames = fixtures.LiveMonitorTests.successful_frames
    setup_pipeline = fixtures.LiveMonitorTests.setup_pipeline

    def install_plan(self, stack, count=3):
        course = group_lessons(lessons(count))[0]
        self.cfg["continuous_course"] = asdict(course)
        stack.enter_context(patch.object(monitor.time, "time", lambda: BASE + self.clock.now))

    def test_live_connection_closes_before_third_opens_and_never_uses_middle_id(self):
        closed, opened = [], []
        with ExitStack() as stack:
            self.install_plan(stack)
            def frames(stream_stop):
                opened.append(f.client.course_id)
                self.assertEqual(len(closed), len(opened) - 1)
                try:
                    yield self.frame
                    self.clock.advance(stream_stop.deadline - BASE - self.clock.now)
                finally:
                    closed.append(f.client.course_id)
            f = self.setup_pipeline(stack, frames)
            f.client.select_lesson.side_effect = lambda cid, end: setattr(f.client, "course_id", cid)
            monitor.cmd_run_live(self.cfg, self.stop)
        self.assertEqual(opened, ["123", "125"])
        self.assertEqual(closed, opened)
        self.assertFalse(self.stop.is_set())
        self.assertEqual(self.stop.waits, [])
        f.asr.flush.assert_not_called()
        f.watcher.discard_partial.assert_called_once()
        f.asr.restart.assert_called_once()
        f.make_asr.assert_called_once()

    def test_two_lesson_class_has_one_stream_and_stops_at_last_end(self):
        with ExitStack() as stack:
            self.install_plan(stack, 2)
            def frames(stream_stop):
                yield self.frame
                self.clock.advance(5820)
            f = self.setup_pipeline(stack, frames)
            monitor.cmd_run_live(self.cfg, self.stop)
        f.client.select_lesson.assert_called_once_with("123", BASE + 5820)
        f.source.assert_called_once()
        f.asr.flush.assert_not_called()

    def test_starting_mid_third_goes_directly_to_third_stream(self):
        with ExitStack() as stack:
            self.install_plan(stack)
            self.clock.advance(6200)
            f = self.setup_pipeline(stack)
            monitor.cmd_run_live(self.cfg, self.stop)
        f.client.select_lesson.assert_called_once_with("125", BASE + 8820)

    def test_preclass_wait_survives_two_minutes_and_connects_after_formal_start(self):
        with ExitStack() as stack:
            self.install_plan(stack)
            self.clock.advance(-120)
            f = self.setup_pipeline(stack)
            def resolve():
                if self.clock.now < 50:
                    raise live.LiveNotStarted("尚未开始")
                return f.stream
            f.client.resolve.side_effect = resolve
            monitor.cmd_run_live(self.cfg, self.stop)
        self.assertEqual(self.clock.now, 54)
        self.assertEqual(self.stop.waits, [6] * 29)
        f.source.assert_called_once()

    def test_preclass_wait_can_be_cancelled(self):
        with ExitStack() as stack:
            self.install_plan(stack)
            self.clock.advance(-120)
            f = self.setup_pipeline(stack)
            f.client.resolve.side_effect = live.LiveNotStarted("尚未开始")
            self.stop.on_wait = self.stop.set
            monitor.cmd_run_live(self.cfg, self.stop)
        f.source.assert_not_called()
        self.assertEqual(self.stop.waits, [6])

    def test_second_cell_preroll_retries_every_six_seconds_without_changing_id(self):
        for count in (2, 3):
            for error in (live.LiveTransientError, live.LiveNotStarted):
                with self.subTest(count=count, error=error), ExitStack() as stack:
                    self.setUp()
                    self.install_plan(stack, count)
                    self.clock.advance(2700)
                    f = self.setup_pipeline(stack)
                    calls = []
                    def resolve():
                        calls.append(self.clock.now)
                        if self.clock.now == 2700:
                            raise error("课间直播未开始")
                        if self.clock.now < 3012:
                            raise live.LiveNotStarted("尚未开始")
                        return f.stream
                    f.client.resolve.side_effect = resolve
                    monitor.cmd_run_live(self.cfg, self.stop)
                self.assertEqual(calls, [2700] + list(range(2880, 3013, 6)))
                self.assertEqual(self.stop.waits, [180] + [6] * 22)
                self.assertEqual([c.args[0] for c in f.client.select_lesson.call_args_list], ["123"])
                f.source.assert_called_once()

    def test_third_cell_preroll_retries_every_six_seconds_with_third_id(self):
        with ExitStack() as stack:
            self.install_plan(stack)
            self.clock.advance(5880)
            f = self.setup_pipeline(stack)
            f.client.resolve.side_effect = [live.LiveNotStarted("尚未开始"), f.stream]
            monitor.cmd_run_live(self.cfg, self.stop)
        self.assertEqual(self.stop.waits, [6])
        f.client.select_lesson.assert_called_once_with("125", BASE + 8820)

    def test_stop_during_first_break_never_opens_second_audio(self):
        with ExitStack() as stack:
            self.install_plan(stack, 2)
            self.clock.advance(2700)
            f = self.setup_pipeline(stack)
            f.client.resolve.side_effect = live.LiveTransientError("课间断流")
            self.stop.on_wait = self.stop.set
            monitor.cmd_run_live(self.cfg, self.stop)
        self.assertEqual(self.stop.waits, [180])
        f.client.resolve.assert_called_once()
        f.source.assert_not_called()

    def test_unstarted_stream_has_bounded_grace_period(self):
        with ExitStack() as stack:
            self.install_plan(stack)
            self.clock.advance(-120)
            f = self.setup_pipeline(stack)
            f.client.resolve.side_effect = live.LiveNotStarted("尚未开始")
            with self.assertRaises(live.LiveNotStarted):
                monitor.cmd_run_live(self.cfg, self.stop)
        self.assertEqual(self.clock.now, 120)

    def test_selected_single_lesson_has_extended_deadline(self):
        with ExitStack() as stack:
            self.cfg["continuous_course"] = asdict(lessons(1)[0])
            stack.enter_context(patch.object(monitor.time, "time", lambda: BASE + self.clock.now))
            def frames(stream_stop):
                yield self.frame
                self.clock.advance(2700)
                self.assertFalse(stream_stop.is_set())
                self.clock.advance(119)
                self.assertFalse(stream_stop.is_set())
                self.clock.advance(1)
                self.assertTrue(stream_stop.is_set())
            f = self.setup_pipeline(stack, frames)
            monitor.cmd_run_live(self.cfg, self.stop)
        f.client.select_lesson.assert_called_once_with("123", BASE + 2820)
        f.source.assert_called_once()

    def test_first_cell_api_end_is_extended_only_by_the_verified_plan(self):
        from test_live import LiveClientTests, live_payload, PAGE, NOW
        fixture = LiveClientTests()
        fixture.setUp()
        try:
            body = live_payload()
            body['data']['liveEndTime'] = (NOW - 1) * 1000
            fixture.response.json.return_value = body
            with live.LiveClient(PAGE) as client:
                with self.assertRaises(live.LiveEnded):
                    client.resolve()
                client.select_lesson('123', NOW + 2700)
                self.assertIsInstance(client.resolve(), live.LiveStream)
        finally:
            fixture.doCleanups()

    def test_terminal_auth_failure_at_boundary_is_not_hidden_by_switch(self):
        with ExitStack() as stack:
            self.install_plan(stack)
            self.clock.advance(5879)
            f = self.setup_pipeline(stack)
            def resolve():
                self.clock.advance(1)
                raise live.LiveLoginRequired('登录过期')
            f.client.resolve.side_effect = resolve
            with self.assertRaises(live.LiveLoginRequired):
                monitor.cmd_run_live(self.cfg, self.stop)
        f.client.select_lesson.assert_called_once_with('123', BASE + 5880)
        f.source.assert_not_called()

    def test_backoff_is_capped_at_the_third_start(self):
        calls = []
        with ExitStack() as stack:
            self.install_plan(stack)
            self.clock.advance(5879)
            f = self.setup_pipeline(stack)
            def resolve():
                calls.append(self.clock.now)
                if len(calls) == 1:
                    raise live.LiveTransientError("暂时断流")
                return f.stream
            f.client.resolve.side_effect = resolve
            monitor.cmd_run_live(self.cfg, self.stop)
        self.assertEqual(self.stop.waits, [1])
        self.assertEqual(calls, [5879, 5880])
        self.assertEqual([c.args[0] for c in f.client.select_lesson.call_args_list], ["123", "125"])

    def test_known_break_waits_are_cancellable_and_do_not_spend_outage_budget(self):
        with ExitStack() as stack:
            self.install_plan(stack)
            self.clock.advance(5700)
            f = self.setup_pipeline(stack)
            f.client.resolve.side_effect = [live.LiveTransientError("课间断流"), f.stream]
            monitor.cmd_run_live(self.cfg, self.stop)
        self.assertEqual(self.stop.waits, [180])
        self.assertEqual([c.args[0] for c in f.client.select_lesson.call_args_list], ["123", "125"])

    def test_stop_during_break_does_not_open_third(self):
        with ExitStack() as stack:
            self.install_plan(stack)
            self.clock.advance(5700)
            f = self.setup_pipeline(stack)
            f.client.resolve.side_effect = live.LiveTransientError("课间断流")
            self.stop.on_wait = self.stop.set
            monitor.cmd_run_live(self.cfg, self.stop)
        f.client.select_lesson.assert_called_once_with("123", BASE + 5880)
        f.source.assert_not_called()

    def test_login_permission_and_certificate_failures_still_stop_in_break(self):
        for error in (live.LiveLoginRequired("登录过期"), live.LiveError("没有权限"),
                      MediaError("证书错误", kind="terminal")):
            with self.subTest(error=error), ExitStack() as stack:
                self.setUp()
                self.install_plan(stack)
                self.clock.advance(5820)
                f = self.setup_pipeline(stack)
                f.client.resolve.side_effect = error
                with self.assertRaises(type(error)):
                    monitor.cmd_run_live(self.cfg, self.stop)
                self.assertEqual(self.stop.waits, [])


if __name__ == "__main__":
    unittest.main()
