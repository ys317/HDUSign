"""Qt presentation contracts. All services and account files are isolated."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import yaml
from PySide6.QtCore import QCoreApplication
from PySide6.QtGui import QGuiApplication
from PySide6.QtTest import QSignalSpy

from hdusign.desktop_controller import Controller, Rows
from hdusign.demo import DemoServices, demo_courses, prepare_demo
from hdusign.live import LiveError, live_course_url


APP = QGuiApplication.instance() or QGuiApplication([])
APP.setQuitOnLastWindowClosed(False)


class DesktopControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        prepare_demo(self.root)
        self.c = Controller(self.root, DemoServices(), auto_load=False)
        self.c.shutdown()  # tests advance queues and clocks explicitly

    def tearDown(self):
        self.c.shutdown()
        if self.c.worker:
            self.c.worker.join(2)
            self.assertFalse(self.c.worker.is_alive())
        self.tmp.cleanup()

    def complete(self):
        deadline = time.monotonic() + 2
        while self.c._task and time.monotonic() < deadline:
            if self.c.worker:
                self.c.worker.join(.01)
            self.c.drain()
        self.assertFalse(self.c._task)

    def select(self, future=False):
        courses = demo_courses()
        self.c.set_courses(courses)
        self.c.selectCourse(live_course_url(courses[int(future)]))
        return courses[int(future)]

    def test_save_preserves_hidden_settings_and_secret_keys(self):
        self.c.cfg['unexposed'] = {'nested': [1, 2]}
        self.c.secrets['retained'] = 'fixture'
        self.c.setField('rules', ' 签到\n\n请签到 ')
        self.c.setField('password', ' spaces stay ')
        self.assertTrue(self.c.save())
        cfg = yaml.safe_load(self.c.config_path.read_text(encoding='utf-8'))
        secrets = json.loads(self.c.secrets_path.read_text())
        self.assertEqual(cfg['unexposed'], {'nested': [1, 2]})
        self.assertEqual(cfg['rules']['strong'], ['签到', '请签到'])
        self.assertEqual(secrets['retained'], 'fixture')
        self.assertNotIn('skl_password', secrets)
        self.assertNotIn(' spaces stay ', self.c.secrets_path.read_text())
        self.assertNotIn('password', str(cfg))


    def test_theme_persists_without_saving_unsaved_fields(self):
        self.c.setField('webhook', 'https://example.invalid/unsaved')
        self.c.toggleTheme()
        cfg = yaml.safe_load(self.c.config_path.read_text(encoding='utf-8'))
        self.assertEqual(cfg['ui']['appearance'], 'light')
        self.assertEqual(cfg['alert']['webhook']['url'], '')
        self.assertTrue(self.c._state['dirty'])
        self.assertEqual(self.c.form.value('webhook'), 'https://example.invalid/unsaved')

    def test_save_failure_is_visible_and_keeps_dirty_form(self):
        self.c.setField('rules', 'updated')
        with patch('hdusign.desktop_controller.write_text_atomic', side_effect=PermissionError()):
            self.assertFalse(self.c.save())
        self.assertTrue(self.c._state['dirty'])
        self.assertTrue(self.c._state['feedbackError'])

    def test_account_change_invalidates_course_and_blocks_monitor(self):
        self.select()
        self.c.setField('username', 'changed')
        self.assertEqual(self.c.course_model.count, 0)
        self.assertEqual(self.c._form['url'], '')
        self.assertTrue(self.c._state['needsLogin'])
        self.assertFalse(self.c.start_monitor())

    def test_login_passes_credentials_snapshot_then_loads_courses(self):
        captured = []
        def login(url, stop, credentials, manual):
            captured.append(copy.deepcopy(credentials))
            return 0
        self.c.services.login = login
        self.c.login(False)
        self.complete()
        self.assertEqual(captured[0]['skl_username'], '2026001001')
        self.assertEqual(self.c.course_model.count, 2)
        self.assertFalse(self.c._state['needsLogin'])

    def test_failed_login_does_not_reuse_previous_selection(self):
        self.select()
        self.c.services.login = lambda *a: 1
        self.c.login(False)
        self.complete()
        self.assertTrue(self.c._state['needsLogin'])
        self.assertEqual(self.c.course_model.count, 0)
        self.assertEqual(self.c._form['url'], '')

    def test_blank_login_focuses_missing_field(self):
        self.c.setField('password', '')
        spy = QSignalSpy(self.c.navigateSettings)
        self.c.login(False)
        self.assertFalse(self.c._task)
        self.assertEqual(spy.at(0), ['password'])

    def test_schedule_survives_restart_and_cancels_on_account_edit(self):
        course = self.select(future=True)
        self.c.toggleMonitor()
        self.assertEqual(self.c._state['actionText'], '取消预约')
        other = Controller(self.root, DemoServices(), auto_load=False)
        other.shutdown()
        self.assertEqual(other._scheduled, course)
        self.assertEqual(other.form.value('url'), live_course_url(course))
        self.assertEqual(other.state.value('courseTitle'), course.title)
        self.assertIn(course.teacher, other.state.value('courseSubtitle'))
        self.c.setField('username', 'changed')
        self.assertIsNone(self.c._scheduled)
        self.assertNotIn('scheduled_course', yaml.safe_load(self.c.config_path.read_text(encoding='utf-8')))

    def test_due_schedule_starts_once_and_clears_persistence(self):
        course = self.select(future=True)
        self.c.toggleMonitor()
        calls = []
        self.c.services.monitor = lambda cfg, stop: (calls.append(cfg), stop.wait(), 0)[-1]
        with patch('hdusign.desktop_controller.time.time', return_value=course.start - 121):
            self.c.tick()
            self.assertFalse(self.c._task)
        with patch('hdusign.desktop_controller.time.time', return_value=course.start - 120):
            self.c.tick()
            self.c.tick()
        self.c.stop()
        self.complete()
        self.assertEqual(len(calls), 1)
        self.assertNotIn('scheduled_course', self.c.cfg)

    def test_expired_schedule_never_starts(self):
        course = self.select(future=True)
        self.c.toggleMonitor()
        with patch('hdusign.desktop_controller.time.time', return_value=course.end + 120):
            self.c.tick()
        self.assertIsNone(self.c._scheduled)
        self.assertFalse(self.c._task)

    def test_continuous_course_snapshot_is_passed_to_monitor(self):
        from hdusign.lessons import group_lessons
        from test_lessons import lessons
        course = group_lessons(lessons())[0]
        self.c.set_courses([course])
        self.c.selectCourse(live_course_url(course))
        captured = []
        self.c.services.monitor = lambda cfg, stop: captured.append(cfg)
        self.assertTrue(self.c.start_monitor())
        self.complete()
        self.assertEqual(captured[0]['continuous_course']['lesson_ids'], ('123', '124', '125'))
        self.assertEqual(self.c.course_model.items[0]['fullTime'], '10-20 10:00–12:25')

    def test_restored_continuous_schedule_keeps_plan_without_refreshed_list(self):
        from hdusign.lessons import group_lessons
        from test_lessons import lessons
        course = group_lessons(lessons())[0]
        self.c.set_courses([course])
        self.c.selectCourse(live_course_url(course))
        # The fixture uses a fixed date. Restore it under the same virtual time
        # used for its scheduled start rather than letting real time expire it.
        with patch('hdusign.desktop_controller.time.time', return_value=course.start - 3600):
            self.c.toggleMonitor()
            other = Controller(self.root, DemoServices(), auto_load=False)
        other.shutdown()
        self.assertEqual(other._scheduled, course)
        captured = []
        other.services.monitor = lambda cfg, stop: captured.append(cfg)
        with patch('hdusign.desktop_controller.time.time', return_value=course.start - 121):
            self.c.tick()
            self.assertFalse(self.c._task)
        with patch('hdusign.desktop_controller.time.time', return_value=course.start - 120):
            other.tick()
        if other.worker:
            other.worker.join(2)
        other.drain()
        self.assertEqual(captured[0]['continuous_course']['lesson_ids'], ('123', '124', '125'))
        other.shutdown()

    def test_cancel_failure_preserves_scheduled_account(self):
        self.select(future=True)
        self.c.toggleMonitor()
        with patch('hdusign.desktop_controller.write_text_atomic', side_effect=PermissionError()):
            self.c.setField('username', 'changed')
        self.assertIsNotNone(self.c._scheduled)
        self.assertEqual(self.c._form['username'], '2026001001')

    def test_partial_burst_is_coalesced_and_final_code_keeps_zero(self):
        spy = QSignalSpy(self.c.changed)
        self.c.consume([f'…识别中: 字 {i}' for i in range(160)] + [
            '[ASR 10:00:00] 签到码是零二三四。', '[i] 签到码: 0234'])
        self.assertEqual(spy.count(), 1)
        self.assertEqual(self.c._state['transcript'], '签到码是零二三四。')
        self.assertTrue(self.c._state['transcriptFinal'])
        self.assertEqual(self.c.log_model.count, 2)
        self.c.copyCode()
        self.assertEqual(APP.clipboard().text(), '0234')
        self.assertEqual(self.c.state.value('code'), '0234')

    def test_reconnect_clears_partial_but_preserves_confirmed_code(self):
        self.c._task = 'monitor'
        self.c.stop_event.clear()
        self.c.consume(['[i] 签到码: 0123', '…识别中: unfinished', '[i] 直播状态: 正在重连'])
        self.assertEqual(self.c._state['code'], '0123')
        self.assertEqual(self.c._state['status'], '正在重连')
        self.c.consume(['[i] ASR 就绪'])
        self.assertEqual(self.c._state['status'], '监控中')
        self.assertEqual(self.c._state['transcript'], '等待课堂声音…')
        self.c._task = ''

    def test_models_are_bounded_and_append_incrementally(self):
        model = Rows(3)
        reset = QSignalSpy(model.modelReset)
        inserted = QSignalSpy(model.rowsInserted)
        model.append([{'n': i} for i in range(3)])
        model.append([{'n': 3}, {'n': 4}])
        self.assertEqual([r['n'] for r in model.items], [2, 3, 4])
        self.assertEqual(reset.count(), 0)
        self.assertEqual(inserted.count(), 2)

    def test_close_waits_for_worker_and_drains_tail_logs(self):
        self.select()
        self.assertTrue(self.c.start_monitor())
        ready = QSignalSpy(self.c.closeReady)
        self.assertFalse(self.c.requestClose())
        self.complete()
        self.assertEqual(ready.count(), 1)
        self.assertEqual(self.c._state['code'], '2330')
        self.assertTrue(self.c.requestClose())

    def test_large_log_tail_delays_completion_without_losing_code(self):
        self.c._task = 'monitor'
        for i in range(400):
            self.c.log_queue.put(f'[i] diagnostic {i}')
        self.c.log_queue.put('[i] 签到码: 0042')
        self.c.result_queue.put(('monitor', False, 0))
        self.c.drain()
        self.assertEqual(self.c._task, 'monitor')
        self.c.drain()
        self.c.drain()
        self.assertFalse(self.c._task)
        self.assertEqual(self.c._state['code'], '0042')


    def test_course_limit_remains_safe_during_tick(self):
        self.c.course_model.limit = 2
        self.c.set_courses(demo_courses() * 10)
        self.c.tick()
        self.assertEqual(len(self.c._courses), 2)

    def test_course_failure_empty_result_and_retry_have_distinct_states(self):
        def denied(cfg, stop):
            raise LiveError('当前账号无权读取直播课程。')
        self.c.services.courses = denied
        self.c.refreshCourses()
        self.assertEqual(self.c._state['courseLoadState'], 'loading')
        self.complete()
        self.assertEqual(self.c._state['status'], '课程加载失败')
        self.assertEqual(self.c._state['courseLoadState'], 'error')
        self.assertEqual(self.c._state['courseError'], '当前账号无权读取直播课程。')
        self.c.services.courses = lambda *args: []
        self.c.refreshCourses()
        self.complete()
        self.assertEqual(self.c._state['courseLoadState'], 'empty')
        self.assertEqual(self.c._state['courseError'], '')
        self.assertEqual(self.c._state['status'], '就绪')
        self.c.services.courses = DemoServices().courses
        self.c.refreshCourses()
        self.complete()
        self.assertEqual(self.c._state['courseLoadState'], 'ready')
        self.assertEqual(self.c.course_model.count, 2)


if __name__ == '__main__':
    unittest.main()
