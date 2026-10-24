"""Course-bound robots, time-window claims, real QML and mocked actual sends."""
from dataclasses import asdict, replace
import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import shiboken6
import yaml
from PySide6.QtCore import QObject, QPointF, Qt, qInstallMessageHandler
from PySide6.QtGui import QGuiApplication
from PySide6.QtTest import QTest

from hdusign.accounts import AccountStore, write_json, protect
from hdusign.alert import Alerter
from hdusign.demo import prepare_demo
from hdusign.live import LiveCourse, live_course_url
from hdusign.multi_account import AccountManager
from hdusign.qt_app import create_engine
from hdusign.reservation_webhooks import binding, binding_url, route_resolver, webhook_key, listening_window, windows_overlap
from test_multi_account import IsolatedFakeServices

APP = QGuiApplication.instance() or QGuiApplication([])
APP.setQuitOnLastWindowClosed(False)


class ReservationRobotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        prepare_demo(self.root)
        store = AccountStore(self.root)
        for username in ('student-a', 'student-b', 'student-c'):
            store.save(username, 'fixture-password')
        self.manager = AccountManager(self.root, services_factory=IsolatedFakeServices, auto_load=False)
        self.controllers = [self.manager.controllers[name] for name in ('student-a', 'student-b', 'student-c')]
        now = time.time()
        self.courses = [LiveCourse(str(901+i), f'课程 {i+1}', now+3600+i*600, now+6300+i*600)
                        for i in range(3)]
        self.urls = [live_course_url(course) for course in self.courses]
        self.hooks = [f'https://example.invalid/robot-{i}' for i in range(4)]
        self.ids = [self.manager.webhooks.remember_verified(f'机器人 {i+1}', url) for i, url in enumerate(self.hooks)]
        for controller in self.manager.controllers.values():
            controller.shutdown()
            controller.set_courses(self.courses)
            self.manager.bind_webhook(controller, self.ids[0])

    def tearDown(self):
        self.manager.shutdown()
        for controller in self.manager.controllers.values():
            if controller.worker:
                controller.worker.join(3)
                self.assertFalse(controller.worker.is_alive())
                controller.drain()
        self.tmp.cleanup()

    def book(self, controller=0, course=0):
        current = self.controllers[controller]
        current.selectCourse(self.urls[course])
        current.toggleMonitor()
        return current

    def choose(self, hook):
        prompt = self.manager.robotPrompt
        self.assertTrue(prompt['visible'])
        self.manager.confirmRobotChoice(prompt['ticket'], webhook_key(self.hooks[hook]))

    def finish(self, controller):
        controller.stop()
        controller.worker.join(3)
        controller.drain()
        self.assertFalse(controller._task)

    def reload(self):
        other = AccountManager(self.root, services_factory=IsolatedFakeServices, auto_load=False)
        for controller in other.controllers.values():
            controller.shutdown()
        self.addCleanup(other.shutdown)
        return other

    def space_courses(self, gap):
        """Place course 2 after course 1; gap is the formal class break."""
        first, second = self.courses[:2]
        start = first.end + gap
        self.courses[1] = replace(second, start=start, end=start + 2700)
        for controller in self.controllers:
            controller.set_courses(self.courses)

    def test_ten_minute_break_allows_same_robot_for_two_accounts_and_restart(self):
        self.space_courses(600)
        first, second = self.book(0, 0), self.book(1, 1)
        self.assertFalse(self.manager.robotPrompt['visible'])
        for current, url in ((first, self.urls[0]), (second, self.urls[1])):
            self.assertEqual(binding_url(current.cfg['reservation_webhooks'][url]), self.hooks[0])
        other = self.reload()
        self.assertFalse(other.robotPrompt['visible'])
        self.assertEqual(binding_url(other.controllers['student-b'].cfg['reservation_webhooks'][self.urls[1]]), self.hooks[0])

    def test_same_account_can_book_non_overlapping_courses_with_same_robot(self):
        self.space_courses(600)
        first = self.book(0, 0)
        self.book(0, 1)
        self.assertEqual(len(first._schedules), 2)
        self.assertFalse(self.manager.robotPrompt['visible'])
        self.assertEqual([binding_url(first.cfg['reservation_webhooks'][url]) for url in self.urls[:2]], self.hooks[:1] * 2)

    def test_four_minute_break_allows_exactly_touching_listen_boundaries(self):
        self.space_courses(240)
        self.book(0, 0)
        second = self.book(1, 1)
        self.assertEqual(len(second._schedules), 1)
        self.assertFalse(self.manager.robotPrompt['visible'])

    def test_less_than_four_minute_break_conflicts_even_without_class_overlap(self):
        self.space_courses(239)
        self.book(0, 0)
        second = self.book(1, 1)
        self.assertFalse(second._schedules)
        self.assertTrue(self.manager.robotPrompt['visible'])
        self.assertNotIn(webhook_key(self.hooks[0]), [v['identity'] for v in self.manager.robotPrompt['choices']])

    def test_canceling_one_shared_booking_keeps_other_booking_claim(self):
        self.space_courses(600)
        first, second = self.book(0, 0), self.book(1, 1)
        first.cancelReservation(self.urls[0])
        claims = self.manager.robot_owners(webhook_key(self.hooks[0]))
        self.assertEqual([claim['account'] for claim in claims], [second.account_username])
        self.book(2, 1)
        self.assertTrue(self.manager.robotPrompt['visible'])

    def test_running_course_allows_later_booking_but_cleanup_holds_robot(self):
        self.space_courses(600)
        first = self.book(0, 0)
        second = self.controllers[1]
        with patch('hdusign.multi_account.time.time', return_value=self.courses[0].start - 120):
            self.assertTrue(first.start_monitor(self.courses[0]))
            self.book(1, 1)
        self.assertFalse(self.manager.robotPrompt['visible'])
        self.assertEqual(len(second._schedules), 1)
        with patch('hdusign.multi_account.time.time', return_value=self.courses[1].start - 120):
            self.assertFalse(self.manager.usable_robot(second, self.urls[1],
                                                      binding('Robot', self.hooks[0]), course=self.courses[1]))
        # Do not rely on the worker reacting to stop before we test its claim.
        first._publish(stopping=True)
        with patch('hdusign.multi_account.time.time', return_value=self.courses[1].start - 120):
            self.assertTrue(second.start_monitor(self.courses[1]))
        self.assertEqual(second._running_webhook['mode'], 'pending')
        self.assertEqual(route_resolver(second.root, second._notification_route)(), '')
        self.finish(first)
        self.manager.confirmRobotChoice(self.manager.robotPrompt['ticket'], webhook_key(self.hooks[0]))
        self.assertEqual(route_resolver(second.root, second._notification_route)(), self.hooks[0])
        self.finish(second)

    def test_early_manual_start_rechecks_interval_instead_of_overlapping_old_course(self):
        self.space_courses(600)
        self.book(0, 0)
        second = self.book(1, 1)
        # Starting the later course before the first course has finished now
        # conflicts even though the originally reserved windows were disjoint.
        with patch('hdusign.multi_account.time.time', return_value=self.courses[0].start):
            self.assertTrue(second.start_monitor(self.courses[1]))
        self.assertEqual(second._running_webhook['mode'], 'pending')
        self.assertEqual(route_resolver(second.root, second._notification_route)(), '')
        self.assertNotIn(webhook_key(self.hooks[0]), [v['identity'] for v in self.manager.robotPrompt['choices']])
        self.manager.confirmRobotChoice(self.manager.robotPrompt['ticket'], webhook_key(self.hooks[0]))
        self.assertEqual(route_resolver(second.root, second._notification_route)(), '')
        self.finish(second)

    def test_unknown_manual_course_time_does_not_assume_robot_is_available(self):
        self.book(0, 0)
        second = self.controllers[1]
        self.assertFalse(self.manager.usable_robot(second, 'manual-course', binding('Robot', self.hooks[0])))

    def test_continuous_block_includes_break_and_last_lesson_end(self):
        first = self.courses[0]
        merged = replace(first, end=first.start + 10500,
                         lesson_ids=('901', '902', '903'))
        gap_course = replace(self.courses[1], start=first.start + 3000, end=first.start + 5700)
        self.assertTrue(windows_overlap(listening_window(merged), listening_window(gap_course)))
        self.assertEqual(listening_window(merged), (first.start - 120, merged.end + 120))

    def test_invalid_course_times_never_establish_non_overlap(self):
        for start, end in ((float('nan'), 5000), (5000, float('inf')), (5000, 4000), (True, 5000)):
            self.assertIsNone(listening_window(replace(self.courses[0], start=start, end=end)))
        self.assertTrue(windows_overlap(None, listening_window(self.courses[0])))

    def test_booking_persists_encrypted_robot_on_the_course_and_survives_restart(self):
        current = self.book()
        value = current.cfg['reservation_webhooks'][self.urls[0]]
        self.assertEqual(binding_url(value), self.hooks[0])
        self.assertEqual(value['name'], '机器人 1')
        saved = yaml.safe_load(current.config_path.read_text('utf-8'))
        self.assertEqual(saved['reservation_webhooks'][self.urls[0]], value)
        self.assertNotIn(self.hooks[0], json.dumps(saved['reservation_webhooks']))
        other = self.reload()
        self.assertEqual(other.controllers['student-a'].cfg['reservation_webhooks'][self.urls[0]], value)
        self.assertFalse(other.robotPrompt['visible'])

    def test_different_accounts_with_overlapping_windows_conflict_and_filter_choices(self):
        self.book(0, 0)
        second = self.book(1, 2)
        prompt = self.manager.robotPrompt
        self.assertTrue(prompt['visible'])
        self.assertIn('student-a', prompt['description'])
        self.assertIn('课程 1', prompt['description'])
        self.assertEqual(second._schedules, [])
        self.assertNotIn(webhook_key(self.hooks[0]), [c['identity'] for c in prompt['choices']])
        self.choose(1)
        self.assertEqual(binding_url(second.cfg['reservation_webhooks'][self.urls[2]]), self.hooks[1])
        self.assertEqual(second.cfg['webhook_binding'], self.ids[0])
        self.assertFalse(self.manager.robotPrompt['visible'])

    def test_two_bookings_in_the_same_account_have_distinct_robots(self):
        first = self.book(0, 0)
        self.book(0, 1)
        self.choose(1)
        self.assertEqual(len(first._schedules), 2)
        self.assertEqual([binding_url(first.cfg['reservation_webhooks'][url]) for url in self.urls[:2]], self.hooks[:2])

    def test_canceling_dialog_does_not_create_or_change_a_booking(self):
        first = self.book(0, 0)
        before = first.config_path.read_bytes()
        second = self.book(1, 1)
        self.manager.cancelRobotChoice(self.manager.robotPrompt['ticket'])
        self.assertFalse(self.manager.robotPrompt['visible'])
        self.assertFalse(second._schedules)
        self.assertEqual(first.config_path.read_bytes(), before)

    def test_no_available_robot_does_not_fall_back_to_an_occupied_one(self):
        self.manager.webhooks.items = {self.ids[0]: self.manager.webhooks.items[self.ids[0]]}
        self.book()
        second = self.book(1, 1)
        self.assertEqual(self.manager.robotPrompt['choices'], [])
        self.manager.confirmRobotChoice(self.manager.robotPrompt['ticket'], webhook_key(self.hooks[0]))
        self.assertFalse(second._schedules)

    def test_cancel_releases_only_that_booking_and_running_task_holds_until_completion(self):
        first = self.book(0, 0)
        first.cancelReservation(self.urls[0])
        self.assertFalse(first.cfg.get('reservation_webhooks'))
        second = self.book(1, 1)
        self.assertEqual(len(second._schedules), 1)
        self.assertTrue(second.start_monitor(self.courses[1]))
        self.assertFalse(second.cfg.get('reservation_webhooks'))
        third = self.book(2, 2)
        self.assertFalse(third._schedules)
        self.assertNotIn(webhook_key(self.hooks[0]), [c['identity'] for c in self.manager.robotPrompt['choices']])
        second.stop()
        self.assertTrue(self.manager.robot_owners(webhook_key(self.hooks[0])))
        self.finish(second)
        self.assertIn(webhook_key(self.hooks[0]), [c['identity'] for c in self.manager.robotPrompt['choices']])
        self.choose(0)
        self.assertEqual(len(third._schedules), 1)

    def test_changing_account_or_global_default_never_changes_existing_booking_or_worker(self):
        current = self.book()
        assigned = copy.deepcopy(current.cfg['reservation_webhooks'][self.urls[0]])
        self.manager.bind_webhook(current, self.ids[1])
        self.manager.setDefaultWebhook(self.ids[2])
        self.assertEqual(current.cfg['reservation_webhooks'][self.urls[0]], assigned)
        current.selectCourse(self.urls[2])
        self.assertTrue(current.start_monitor(self.courses[0]))
        deadline = time.monotonic()+2
        while not current.services.calls and time.monotonic() < deadline:
            time.sleep(.001)
        cfg = current.services.calls[0]
        self.assertEqual(cfg['alert']['webhook']['url'], self.hooks[0])
        self.assertEqual(cfg['live_url'], self.urls[0])
        self.assertEqual(cfg['notification_context']['course_name'], self.courses[0].title)
        self.assertEqual(route_resolver(current.root, cfg['_notification_route'])(), self.hooks[0])
        self.finish(current)

    def test_confirm_rechecks_a_stale_choice_instead_of_double_claiming(self):
        self.book(0, 0)
        second = self.book(1, 1)
        ticket = self.manager.robotPrompt['ticket']
        third = self.controllers[2]
        self.manager.bind_webhook(third, self.ids[1])
        self.book(2, 2)
        self.manager.confirmRobotChoice(ticket, webhook_key(self.hooks[1]))
        self.assertFalse(second._schedules)
        self.assertTrue(self.manager.robotPrompt['visible'])
        self.assertNotIn(webhook_key(self.hooks[1]), [c['identity'] for c in self.manager.robotPrompt['choices']])

    def test_bind_write_failure_keeps_all_claims_and_old_configuration(self):
        current = self.book()
        before = current.config_path.read_bytes()
        second = self.book(1, 1)
        with patch('hdusign.desktop_controller.write_text_atomic', side_effect=PermissionError()):
            self.choose(1)
        self.assertEqual(current.config_path.read_bytes(), before)
        self.assertEqual(binding_url(current.cfg['reservation_webhooks'][self.urls[0]]), self.hooks[0])
        self.assertFalse(second._schedules)
        self.assertTrue(self.manager.robotPrompt['visible'])
        self.assertFalse(self.manager.robot_owners(webhook_key(self.hooks[1])))

    def test_legacy_conflicts_are_pending_and_can_enable_notifications_after_local_start(self):
        for current, course in zip(self.controllers[:2], self.courses[:2]):
            cfg = dict(current.cfg, scheduled_courses=[asdict(course)])
            cfg.pop('reservation_webhooks', None)
            current.config_path.write_text(yaml.safe_dump(cfg, allow_unicode=True), 'utf-8')
        other = self.reload()
        first, second = [other.controllers[name] for name in ('student-a', 'student-b')]
        self.assertEqual(first.cfg['reservation_webhooks'][self.urls[0]]['mode'], 'bound')
        self.assertEqual(second.cfg['reservation_webhooks'][self.urls[1]]['mode'], 'pending')
        self.assertTrue(other.robotPrompt['visible'])
        self.assertTrue(second.start_monitor(self.courses[1]))
        try:
            resolver = route_resolver(second.root, second._notification_route)
            self.assertEqual(resolver(), '')
            other.confirmRobotChoice(other.robotPrompt['ticket'], webhook_key(self.hooks[1]))
            self.assertEqual(resolver(), self.hooks[1])
            self.assertEqual(second._running_webhook['name'], '机器人 2')
            self.assertFalse(other.robotPrompt['visible'])
            self.assertTrue(other.robot_owners(webhook_key(self.hooks[1])))
        finally:
            self.finish(second)

    def test_legacy_reservations_keep_robot_when_no_conflict_and_persist_binding(self):
        current = self.controllers[0]
        cfg = dict(current.cfg, scheduled_course=asdict(self.courses[0]))
        cfg.pop('scheduled_courses', None)
        current.config_path.write_text(yaml.safe_dump(cfg, allow_unicode=True), 'utf-8')
        other = self.reload()
        saved = other.controllers['student-a'].cfg
        self.assertEqual(binding_url(saved['reservation_webhooks'][self.urls[0]]), self.hooks[0])
        self.assertNotIn('scheduled_course', saved)

    def test_equivalent_wechat_urls_are_one_robot_even_with_different_names(self):
        a = 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=fixture&extra=1'
        b = 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?extra=2&key=fixture#ignored'
        self.assertEqual(webhook_key(a), webhook_key(b))
        common = self.manager.webhooks.remember_verified('别名甲', a)
        self.manager.webhooks.remember_verified('别名乙', b)
        for current in self.controllers[:2]:
            self.manager.bind_webhook(current, common)
        self.book(0, 0)
        self.book(1, 1)
        self.assertNotIn(webhook_key(a), [c['identity'] for c in self.manager.robotPrompt['choices']])

    def test_unverified_robots_never_appear_as_available_choices(self):
        self.manager.webhooks.items[self.ids[2]]['verified'] = False
        self.book()
        self.book(1, 1)
        self.assertNotIn(webhook_key(self.hooks[2]), [c['identity'] for c in self.manager.robotPrompt['choices']])

    def test_overlapping_booking_holds_robot_until_canceled(self):
        current = self.book()
        self.manager.chooseReservationRobot(current.account_username, self.urls[0])
        self.assertFalse(self.manager.robotPrompt['visible'])
        self.assertTrue(self.manager.robot_owners(webhook_key(self.hooks[0])))
        current.cancelReservation(self.urls[0])
        self.manager.bind_webhook(current, self.ids[1])
        self.book()
        self.assertFalse(self.manager.robot_owners(webhook_key(self.hooks[0])))
        self.assertEqual(current.reservations[0]['robotName'], '机器人 2')

    def test_deleting_account_releases_its_reserved_robot(self):
        current = self.book()
        self.manager.deleteAccount(current.account_username)
        self.assertFalse(self.manager.robot_owners(webhook_key(self.hooks[0])))
        self.book(1, 1)
        self.assertFalse(self.manager.robotPrompt['visible'])

    def test_qml_popup_lists_only_free_robots_and_confirm_binds_the_new_course(self):
        messages = []
        previous = qInstallMessageHandler(lambda kind, ctx, message: messages.append(message))
        engine = None
        try:
            self.book()
            self.manager.activate('student-b')
            engine, window = create_engine(self.manager, APP)
            self.book(1, 1)
            QTest.qWait(70)
            dialog = window.findChild(QObject, 'robotConflictDialog')
            self.assertTrue(dialog.property('visible'))
            box = window.findChild(QObject, 'availableRobotBox')
            self.assertEqual(box.property('count'), 3)
            confirm = window.findChild(QObject, 'confirmRobotChoiceButton')
            self.assertFalse(confirm.property('enabled'))
            choices = self.manager.robotPrompt['choices']
            dialog.setProperty('selectedIdentity', choices[0]['identity'])
            QTest.qWait(20)
            pos = confirm.mapToScene(QPointF(confirm.width()/2, confirm.height()/2)).toPoint()
            QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pos)
            QTest.qWait(20)
            self.assertEqual(len(self.controllers[1]._schedules), 1)
            self.assertFalse(dialog.property('visible'))
        finally:
            if engine:
                shiboken6.delete(engine)
            qInstallMessageHandler(previous)
        self.assertEqual(messages, [])


class NotificationRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root/'notification_routes'/'fixture.json'
        self.path.parent.mkdir()
        self.spec = {'path': str(self.path), 'run_id': 'fixture-run'}
        self.hook = 'https://example.invalid/assigned-course'

    def route(self, value):
        write_json(self.path, {'run_id': 'fixture-run', 'binding': value})

    def test_actual_request_uses_course_route_and_preserves_name_course_for_all_levels(self):
        self.route(binding('本门课机器人', self.hook))
        alerter = Alerter(webhook_url='https://example.invalid/WRONG-ACCOUNT-DEFAULT',
                          student_name='张同学', course_name='金融科技概论',
                          webhook_resolver=route_resolver(self.root, self.spec))
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"errcode":0}'
        with patch('urllib.request.urlopen', return_value=response) as send:
            for level in ('code', 'high', 'test'):
                self.assertTrue(alerter._send_wechat('10:00:00', level, '测试', '原句'))
                request = send.call_args.args[0]
                self.assertEqual(request.full_url, self.hook)
                text = json.loads(request.data)['markdown']['content']
                self.assertIn('张同学', text)
                self.assertIn('金融科技概论', text)

    def test_pending_or_broken_route_never_uses_the_account_fallback(self):
        resolver = route_resolver(self.root, self.spec)
        alerter = Alerter(webhook_url='https://example.invalid/WRONG', webhook_resolver=resolver)
        for value in (binding(pending=True), binding(), {'mode': 'bound', 'encrypted_url': 'broken', 'key': 'broken'}):
            self.route(value)
            with patch('urllib.request.urlopen') as send:
                self.assertFalse(alerter._send_wechat('10:00:00', 'code', '原因', '原句'))
                send.assert_not_called()
        self.path.write_text('[]', encoding='utf-8')
        self.assertEqual(resolver(), '')
        self.path.unlink()
        self.assertEqual(resolver(), '')

    def test_route_checks_root_and_run_identity_and_can_switch_from_pending_to_bound(self):
        self.route(binding(pending=True))
        resolver = route_resolver(self.root, self.spec)
        self.assertEqual(resolver(), '')
        self.route(binding('人工选定', self.hook))
        self.assertEqual(resolver(), self.hook)
        self.assertEqual(route_resolver(self.root, dict(self.spec, run_id='wrong'))(), '')
        self.assertEqual(route_resolver(self.root/'other-account', self.spec)(), '')

    def test_notification_thread_must_finish_before_worker_process_can_exit(self):
        alerter = Alerter(log_file=str(self.root/'alerts.jsonl'), webhook_url=self.hook)
        with patch('hdusign.alert.threading.Thread') as thread:
            alerter.notify('确认签到码', 'code', '测试')
            self.assertIs(thread.call_args.kwargs['daemon'], False)


if __name__ == '__main__':
    unittest.main()
