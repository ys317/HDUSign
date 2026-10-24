"""Independent browsing, durable queues and the actual one-worker handoff."""
import copy
from dataclasses import asdict
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import shiboken6
import yaml
from PySide6.QtCore import QObject, QPointF, Qt, qInstallMessageHandler
from PySide6.QtGui import QGuiApplication
from PySide6.QtTest import QTest

from hdusign.accounts import AccountStore
from hdusign.desktop_controller import Controller
from hdusign.demo import prepare_demo, DemoServices
from hdusign.live import LiveCourse, live_course_url
from hdusign.multi_account import AccountManager
from hdusign.qt_app import create_engine
from test_multi_account import IsolatedFakeServices

APP = QGuiApplication.instance() or QGuiApplication([])
APP.setQuitOnLastWindowClosed(False)


class HoldingService:
    isolated = True

    def __init__(self):
        self.calls = []

    def monitor(self, cfg, stop):
        self.calls.append(copy.deepcopy(cfg))
        stop.wait()
        return 0


class MultipleReservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        prepare_demo(self.root)
        self.c = Controller(self.root, HoldingService(), auto_load=False)
        self.c.shutdown()
        now = time.time()
        self.courses = [LiveCourse(str(800 + i), title, now + 3600 + i * 10800,
                                   now + 6300 + i * 10800, tecl_id="456")
                        for i, title in enumerate(("金融科技概论", "习概", "统计学概论"))]
        self.c.set_courses(self.courses)

    def tearDown(self):
        self.c.shutdown()
        if self.c.worker:
            self.c.worker.join(2)
        self.tmp.cleanup()

    def select(self, i):
        self.c.selectCourse(live_course_url(self.courses[i]))

    def book(self, i):
        self.select(i)
        self.c.toggleMonitor()

    def finish(self):
        self.c.stop()
        if self.c.worker:
            self.c.worker.join(2)
        while self.c._task:
            self.c.drain()

    def restored(self):
        other = Controller(self.root, HoldingService(), auto_load=False)
        other.shutdown()
        self.addCleanup(other.shutdown)
        return other

    def test_browsing_never_cancels_or_persists_a_different_reservation(self):
        self.book(1)
        saved = self.c.config_path.read_bytes()
        for i in (0, 2, 1, 0):
            self.select(i)
            self.c.tick()
            self.assertEqual(self.c._schedules, [self.courses[1]])
            self.assertEqual(self.c._state['courseTitle'], self.courses[i].title)
        self.assertEqual(self.c.config_path.read_bytes(), saved)
        self.assertEqual(self.c._state['actionText'], '预约直播')
        self.assertEqual(self.c._state['reservationCount'], 1)

    def test_multiple_bookings_sort_restore_and_cancel_only_the_chosen_course(self):
        for i in (2, 0, 1):
            self.book(i)
        self.assertEqual(self.c._schedules, self.courses)
        self.assertEqual(self.restored()._schedules, self.courses)
        self.select(1)
        self.c.toggleMonitor()
        self.assertEqual(self.c._schedules, [self.courses[0], self.courses[2]])
        self.assertEqual(self.restored()._schedules, self.c._schedules)
        self.assertTrue(self.c.course_model.items[0]['reserved'])
        self.assertFalse(self.c.course_model.items[1]['reserved'])
        self.assertTrue(self.c.course_model.items[2]['reserved'])

    def test_cancel_from_queue_does_not_change_the_view(self):
        self.book(0)
        self.book(1)
        self.select(2)
        self.c.cancelReservation(live_course_url(self.courses[0]))
        self.assertEqual(self.c._schedules, [self.courses[1]])
        self.assertEqual(self.c._state['selectedUrl'], live_course_url(self.courses[2]))

    def test_due_course_uses_its_own_url_metadata_and_notification_name(self):
        self.book(0)
        self.book(1)
        self.select(2)
        with patch('hdusign.desktop_controller.time.time', return_value=self.courses[0].start - 120):
            self.c.tick()
            self.c.tick()
        self.finish()
        cfg = self.c.services.calls[0]
        self.assertEqual(len(self.c.services.calls), 1)
        self.assertEqual(cfg['live_url'], live_course_url(self.courses[0]))
        self.assertEqual(cfg['continuous_course']['course_id'], self.courses[0].course_id)
        self.assertEqual(cfg['notification_context']['course_name'], self.courses[0].title)
        self.assertEqual(self.c._state['selectedUrl'], live_course_url(self.courses[2]))
        self.assertEqual(self.restored()._schedules, [self.courses[1]])

    def test_worker_completion_then_next_due_course_starts_once(self):
        self.book(0)
        self.book(1)
        self.select(2)
        for course in self.courses[:2]:
            with patch('hdusign.desktop_controller.time.time', return_value=course.start - 120):
                self.c.tick()
                self.c.tick()
            self.assertEqual(self.c._task, 'monitor')
            self.finish()
        self.assertEqual([cfg['live_url'] for cfg in self.c.services.calls],
                         [live_course_url(course) for course in self.courses[:2]])
        self.assertEqual(self.c._schedules, [])
        self.assertNotIn('scheduled_courses', self.c.cfg)

    def test_live_transcript_code_and_logs_stay_with_the_running_course(self):
        self.select(0)
        self.assertTrue(self.c.start_monitor())
        self.select(1)
        self.c.consume(['[i] ASR 就绪', '[ASR 10:00:01] 第一门课程原文', '[i] 签到码: 0123'])
        self.assertEqual(self.c._state['code'], '')
        self.assertNotIn('第一门', self.c._state['transcript'])
        self.assertEqual(self.c.log_model.count, 0)
        self.assertEqual(self.c.activity_model.count, 0)
        self.assertTrue(self.c._state['accountMonitoring'])
        self.assertFalse(self.c._state['monitoring'])
        self.c.toggleMonitor()  # booking the viewed future course during A's worker
        self.assertEqual(self.c._schedules, [self.courses[1]])
        self.select(0)
        self.assertEqual(self.c._state['code'], '0123')
        self.assertEqual(self.c._state['transcript'], '第一门课程原文')
        self.assertEqual(self.c.log_model.count, 3)
        self.assertEqual(self.c.activity_model.count, 1)
        self.assertTrue(self.c._state['monitoring'])
        self.select(2)
        self.finish()
        self.select(0)
        self.assertEqual(self.c._state['status'], '已停止')
        self.assertEqual(self.c._state['code'], '0123')
        self.assertEqual(len(self.c.services.calls), 1)

    def test_clearing_another_courses_logs_does_not_clear_the_running_course(self):
        self.select(0)
        self.c.start_monitor()
        self.c.consume(['[ASR 00:00:00] 保留第一门转写'])
        self.select(1)
        self.c.clearActivity()
        self.select(0)
        self.assertIn('保留第一门', self.c.log_model.items[0]['text'])
        self.finish()

    def test_cancel_and_add_write_failures_preserve_the_entire_queue(self):
        self.book(0)
        self.book(1)
        original = self.c.config_path.read_bytes()
        with patch('hdusign.desktop_controller.write_text_atomic', side_effect=PermissionError()):
            self.c.cancelReservation(live_course_url(self.courses[0]))
            self.book(2)
        self.assertEqual(self.c._schedules, self.courses[:2])
        self.assertEqual(self.c.config_path.read_bytes(), original)
        self.assertEqual(self.c._state['reservationCount'], 2)

    def test_legacy_singleton_migrates_without_losing_continuous_lesson_metadata(self):
        from hdusign.lessons import group_lessons
        from test_lessons import lessons
        course = group_lessons(lessons())[0]
        cfg = self.c.cfg.copy()
        cfg['scheduled_course'] = asdict(course)
        self.c.config_path.write_text(yaml.safe_dump(cfg), encoding='utf-8')
        with patch('hdusign.desktop_controller.time.time', return_value=course.start - 3600):
            other = self.restored()
        self.assertEqual(other._schedules, [course])
        self.assertNotIn('scheduled_course', other.cfg)
        self.assertEqual(other._scheduled.lesson_ids, ('123', '124', '125'))

    def test_expiration_removes_only_expired_bookings_and_leaves_later_course(self):
        self.book(0)
        self.book(1)
        with patch('hdusign.desktop_controller.time.time', return_value=self.courses[0].end + 120):
            self.c.tick()
        self.assertEqual(self.c._schedules, [self.courses[1]])
        self.assertFalse(self.c._task)

    def test_manual_url_edits_and_relogin_preserve_the_bookings(self):
        self.book(0)
        self.book(1)
        self.c.setField('url', live_course_url(self.courses[2]))
        self.assertEqual(self.c._schedules, self.courses[:2])
        self.c.services = DemoServices()
        self.c.login()
        deadline = time.monotonic() + 3
        while self.c._task and time.monotonic() < deadline:
            self.c.worker.join(.01)
            self.c.drain()
        self.assertFalse(self.c._task)
        self.assertEqual(self.c._schedules, self.courses[:2])

    def test_many_accounts_keep_multiple_reservations_and_runtime_summaries_separate(self):
        store = AccountStore(self.root)
        store.save('fixture-a', 'fixture-password-a')
        store.save('fixture-b', 'fixture-password-b')
        manager = AccountManager(self.root, services_factory=IsolatedFakeServices, auto_load=False)
        try:
            for controller in manager.controllers.values():
                controller.shutdown()
            first = manager.controllers['fixture-a']
            second = manager.controllers['fixture-b']
            for controller, courses in ((first, self.courses[:2]), (second, self.courses[1:])):
                controller.set_courses(courses)
                for course in courses:
                    controller.selectCourse(live_course_url(course))
                    controller.toggleMonitor()
            first.start_monitor(self.courses[0])
            first.selectCourse(live_course_url(self.courses[1]))
            rows = {r['username']: r for r in manager.accountRows}
            self.assertEqual(rows['fixture-a']['course'], self.courses[0].title)
            self.assertTrue(rows['fixture-a']['monitoring'])
            self.assertEqual(rows['fixture-b']['status'], '已预约 2 门')
            self.assertEqual(second._schedules, self.courses[1:])
            self.assertEqual(first._schedules, [self.courses[1]])
            manager.stopAll()
        finally:
            manager.shutdown()
            for controller in manager.controllers.values():
                if controller.worker:
                    controller.worker.join(2)
                    controller.drain()

    def test_actual_qml_can_book_view_and_cancel_with_a_background_monitor(self):
        messages = []
        previous = qInstallMessageHandler(lambda kind, ctx, message: messages.append(message))
        engine = None
        try:
            self.book(0)
            self.book(1)
            engine, window = create_engine(self.c, APP)
            QTest.qWait(100)
            panel = window.findChild(QObject, 'reservationPanel')
            self.assertTrue(panel.property('visible'))
            button = window.findChild(QObject, 'monitorButton')
            self.select(2)
            QTest.qWait(30)
            position = button.mapToScene(QPointF(button.width()/2, button.height()/2)).toPoint()
            QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, position)
            self.assertEqual(self.c._schedules, self.courses)
            self.c.start_monitor(self.courses[0])
            QTest.qWait(30)
            self.assertTrue(button.property('enabled'))  # selected C: cancel reservation
            returning = window.findChild(QObject, 'returnToMonitor')
            self.assertTrue(returning.property('visible'))
            self.c.cancelReservation(live_course_url(self.courses[1]))
            self.assertEqual(self.c._schedules, [self.courses[2]])
            self.c.selectCourse(live_course_url(self.courses[0]))
            QTest.qWait(30)
            self.assertEqual(button.property('text'), '停止监控')
            self.finish()
        finally:
            if engine:
                shiboken6.delete(engine)
            qInstallMessageHandler(previous)
        self.assertEqual(messages, [])


if __name__ == '__main__':
    unittest.main()
