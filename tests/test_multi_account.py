"""Multi-account boundaries, schedules and verified webhook persistence."""
import copy
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, Mock

import yaml
from PySide6.QtCore import QCoreApplication
from PySide6.QtGui import QGuiApplication

from hdusign.accounts import AccountStore, SESSION_ENV, read_json, write_json
from hdusign.account_worker import ProcessServices
from hdusign.demo import prepare_demo, demo_courses
from hdusign.live import live_course_url
from hdusign.multi_account import AccountManager, WebhookStore
from hdusign.alert import Alerter

APP = QGuiApplication.instance() or QGuiApplication([])
APP.setQuitOnLastWindowClosed(False)


class IsolatedFakeServices:
    isolated = True
    def __init__(self, root):
        self.root = root
        self.calls = []
        self.log = lambda line: None
        self.fail = False
        self.webhook_result = 0

    def monitor(self, cfg, stop):
        self.calls.append(copy.deepcopy(cfg))
        self.log('[ASR 10:00:01] 独立转写 ' + self.credentials['skl_username'])
        if self.fail:
            raise RuntimeError('isolated fixture failure')
        stop.wait()

    def courses(self, cfg, stop):
        return demo_courses()

    def login(self, url, stop, credentials, manual):
        write_json(self.root / 'live_session.json', {'username': credentials['skl_username'],
                   'user_code': credentials['skl_username'], 'display_name': '测试同学'})
        return 0

    def webhook(self, cfg, stop):
        self.calls.append(copy.deepcopy(cfg))
        return self.webhook_result


class MultiAccountTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        prepare_demo(self.root)
        store = AccountStore(self.root)
        for i in range(6):
            store.save('student-' + str(i), 'fixture-password-' + str(i))
        self.manager = AccountManager(self.root, services_factory=IsolatedFakeServices, auto_load=False)
        for controller in self.manager.controllers.values():
            controller._startup_timer.stop()

    def tearDown(self):
        self.manager.shutdown()
        for controller in self.manager.controllers.values():
            if controller.worker:
                controller.worker.join(3)
                self.assertFalse(controller.worker.is_alive())
        self.tmp.cleanup()

    def drain(self, condition=lambda: True):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            for controller in list(self.manager.controllers.values()):
                controller.drain()
            QCoreApplication.processEvents()
            if condition():
                return
            time.sleep(.005)
        self.fail('Account tasks did not reach expected state')

    def select(self, username, future=False):
        self.manager.activate(username)
        controller = self.manager.active
        controller.set_courses(demo_courses())
        controller.selectCourse(live_course_url(controller._courses[int(future)]))
        return controller

    def test_five_parallel_tasks_have_distinct_credentials_logs_and_stop(self):
        before = os.environ.get(SESSION_ENV)
        controllers = [self.select('student-' + str(i)) for i in range(5)]
        for controller in controllers:
            self.assertTrue(controller.start_monitor())
        self.drain(lambda: all(c.services.calls for c in controllers))
        self.assertEqual(os.environ.get(SESSION_ENV), before)
        sixth = self.select('student-5')
        self.assertFalse(sixth.start_monitor())
        self.assertIn('5', sixth._state['feedback'])
        self.manager.stopAccount('student-1')
        self.drain(lambda: not controllers[1]._task)
        self.assertTrue(all(c._task == 'monitor' for i, c in enumerate(controllers) if i != 1))
        for i, controller in enumerate(controllers):
            text = ' '.join(item['text'] for item in controller.log_model.items)
            self.assertIn('student-' + str(i), text)
            self.assertNotIn('student-' + str((i + 1) % 5), text)
        self.assertTrue(sixth.start_monitor())
        self.manager.stopAll()
        self.drain(lambda: self.manager.monitor_count() == 0)

    def test_independent_reservations_survive_restart(self):
        first = self.select('student-0', True)
        second = self.select('student-1', True)
        for controller in (first, second):
            controller.toggleMonitor()
            self.assertTrue(controller._scheduled)
        first.cancelSchedule()
        self.assertTrue(second._scheduled)
        other = AccountManager(self.root, services_factory=IsolatedFakeServices, auto_load=False)
        try:
            self.assertIsNone(other.controllers['student-0']._scheduled)
            self.assertEqual(other.controllers['student-1']._scheduled, second._scheduled)
        finally:
            other.shutdown()

    def test_failed_account_does_not_stop_another(self):
        first = self.select('student-0')
        second = self.select('student-1')
        first.services.fail = True
        first.start_monitor()
        second.start_monitor()
        self.drain(lambda: not first._task)
        self.assertEqual(first._state['status'], '监控异常')
        self.assertEqual(second._task, 'monitor')

    def test_new_login_saves_only_in_its_account_root(self):
        self.manager.activate('')
        draft = self.manager.active
        draft.setField('username', 'new-student')
        draft.setField('password', 'new-password')
        draft.login(False)
        target = self.manager.active
        self.drain(lambda: not target._task)
        self.assertEqual(target.account_username, 'new-student')
        self.assertEqual(read_json(target.root / 'live_session.json')['username'], 'new-student')
        self.assertIn('new-student', self.manager.accounts.accounts)
        self.assertFalse((draft.root / 'live_session.json').exists())
        self.assertFalse((self.root / 'live_session.json').exists())

    def test_webhook_is_saved_only_after_success_and_can_be_named_and_assigned(self):
        first = self.select('student-0')
        first.setField('webhookName', '个人提醒')
        first.setField('webhook', 'https://example.invalid/robot-a')
        first.save()
        self.assertNotIn('robot-a', first.config_path.read_text('utf-8'))
        first.services.webhook_result = 1
        first.runAction('webhook')
        self.drain(lambda: not first._task)
        self.assertEqual(self.manager.webhooks.items, {})
        first.services.webhook_result = 0
        first.runAction('webhook')
        self.drain(lambda: not first._task)
        identity = first.cfg['webhook_binding']
        self.assertEqual(self.manager.webhooks.items[identity]['name'], '个人提醒')
        self.assertEqual(first.services.calls[-1]['alert']['webhook']['url'], 'https://example.invalid/robot-a')
        self.assertNotIn('robot-a', self.manager.webhooks.path.read_text('utf-8'))
        second = self.select('student-1')
        self.assertEqual(second._form['webhook'], 'https://example.invalid/robot-a')
        self.assertEqual(second.cfg.get('webhook_binding', ''), '')
        self.manager.assignWebhook(identity)
        self.assertEqual(second._form['webhook'], 'https://example.invalid/robot-a')
        first.setField('webhookName', ' renamed ')
        first.runAction('webhook')
        self.drain(lambda: not first._task)
        self.assertEqual(self.manager.webhooks.items[identity]['name'], 'renamed')
        reloaded = WebhookStore(self.root)
        self.assertEqual(reloaded.url(identity), 'https://example.invalid/robot-a')

    def test_default_and_private_webhooks_do_not_change_running_snapshot(self):
        hooks = self.manager.webhooks
        common = hooks.remember_verified('公用', 'https://example.invalid/common')
        private = hooks.remember_verified('私人', 'https://example.invalid/private')
        first = self.select('student-0')
        second = self.select('student-1')
        self.manager.bind_webhook(first, private)
        self.manager.bind_webhook(second, '')
        first.start_monitor()
        second.start_monitor()
        self.drain(lambda: first.services.calls and second.services.calls)
        self.manager.setDefaultWebhook(private)
        self.assertEqual(first.services.calls[0]['alert']['webhook']['url'], 'https://example.invalid/private')
        self.assertEqual(second.services.calls[0]['alert']['webhook']['url'], 'https://example.invalid/common')
        self.assertEqual(second._form['webhook'], 'https://example.invalid/private')

    def test_selecting_webhook_does_not_mark_unrelated_edits_saved(self):
        controller = self.select('student-0')
        controller.setField('rules', 'uncommitted-rules')
        identity = self.manager.webhooks.remember_verified('可用机器人', 'https://example.invalid/available')
        self.manager.assignWebhook(identity)
        self.assertTrue(controller._state['dirty'])
        self.assertNotIn('uncommitted-rules', controller.config_path.read_text('utf-8'))
        controller.setField('webhookName', '尚未确认的名称')
        controller.save()
        self.assertTrue(controller._state['dirty'])
        self.assertEqual(self.manager.webhooks.items[identity]['name'], '可用机器人')

    def test_delete_one_account_preserves_other_account_data(self):
        first = self.select('student-0')
        second = self.select('student-1')
        for controller in (first, second):
            write_json(controller.root / 'live_session.json', {'username': controller.account_username})
        self.manager.deleteAccount('student-0')
        self.assertFalse(first.root.exists())
        self.assertTrue(second.root.exists())
        self.assertIn('student-1', self.manager.accounts.accounts)

    def test_close_waits_for_every_account(self):
        first = self.select('student-0')
        second = self.select('student-1')
        first.start_monitor()
        second.start_monitor()
        self.assertFalse(self.manager.requestClose())
        self.drain(lambda: not first._task and not second._task)

    def test_recent_account_order_survives_restart_without_changing_login_default(self):
        legacy_order = list(self.manager.controllers)
        self.assertEqual([row['username'] for row in self.manager.accountRows], legacy_order)
        default = self.manager.accounts.last
        credentials = dict(self.manager.accounts.accounts)
        with patch('hdusign.multi_account.time.time', return_value=time.time()):
            self.manager.activate('student-1')
            self.manager.activate('student-0')
        expected = ['student-0', 'student-1'] + [name for name in legacy_order
                                               if name not in ('student-0', 'student-1')]
        self.assertEqual([row['username'] for row in self.manager.accountRows], expected)
        self.assertIs(self.manager.active, self.manager.controllers['student-0'])
        self.assertEqual(self.manager.accounts.accounts, credentials)
        self.assertEqual(self.manager.accounts.last, default)
        before = self.manager.accounts.path.read_bytes()
        other = AccountManager(self.root, services_factory=IsolatedFakeServices, auto_load=False)
        try:
            self.assertEqual([row['username'] for row in other.accountRows], expected)
            self.assertEqual(other.active.account_username, default)
            for controller in other.controllers.values():
                controller.tick()
                controller.set_courses(demo_courses())
            self.assertEqual(other.accounts.path.read_bytes(), before)
            self.assertEqual([row['username'] for row in other.accountRows], expected)
        finally:
            other.shutdown()

    def test_successful_booking_and_monitor_start_update_account_order(self):
        first = self.select('student-0', future=True)
        self.manager.activate('student-1')
        first.toggleMonitor()
        self.assertTrue(first._scheduled)
        self.assertEqual(self.manager.accountRows[0]['username'], 'student-0')
        second = self.select('student-1')
        self.manager.activate('student-2')
        self.assertTrue(second.start_monitor())
        self.assertEqual([row['username'] for row in self.manager.accountRows[:2]],
                         ['student-0', 'student-1'])
        self.assertIs(self.manager.active, self.manager.controllers['student-2'])
        self.assertTrue(first._scheduled)
        self.assertEqual(second._task, 'monitor')

    def test_reservations_rank_before_recent_accounts_and_cancel_releases_priority(self):
        first = self.select('student-0', future=True)
        first.toggleMonitor()
        second = self.select('student-1', future=True)
        second.toggleMonitor()
        self.manager.activate('student-2')
        self.assertEqual([row['username'] for row in self.manager.accountRows[:3]],
                         ['student-1', 'student-0', 'student-2'])
        self.manager.activate('student-0')
        self.assertEqual([row['username'] for row in self.manager.accountRows[:3]],
                         ['student-0', 'student-1', 'student-2'])
        first.cancelSchedule()
        self.assertEqual([row['username'] for row in self.manager.accountRows[:3]],
                         ['student-1', 'student-0', 'student-2'])

    def test_failed_booking_and_monitor_start_keep_recent_order(self):
        first = self.select('student-0', future=True)
        self.manager.activate('student-1')
        before = self.manager.accounts.path.read_bytes()
        with patch.object(first, 'save', return_value=False):
            first.toggleMonitor()
            self.assertFalse(first.start_monitor())
        self.assertFalse(first._schedules)
        self.assertEqual(self.manager.accountRows[0]['username'], 'student-1')
        self.assertEqual(self.manager.accounts.path.read_bytes(), before)

    def test_successful_login_updates_usage_and_unremembered_login_preserves_history(self):
        first = self.select('student-0')
        first.login(False)
        self.manager.activate('student-1')
        self.drain(lambda: bool(self.manager.accounts.last_used.get('student-0'))
                   and not first._task)
        self.assertEqual(self.manager.accountRows[0]['username'], 'student-0')
        previous = dict(self.manager.accounts.last_used)
        self.manager.activate('')
        draft = self.manager.active
        draft.setField('username', 'temporary-student')
        draft.setField('password', 'fixture-password')
        draft.setField('remember', False)
        draft.login(False)
        target = self.manager.active
        self.drain(lambda: not target._task)
        self.assertEqual(self.manager.accountRows[0]['username'], 'temporary-student')
        vault = AccountStore(self.root)
        self.assertNotIn('temporary-student', vault.accounts)
        self.assertEqual(vault.last_used, previous)

    def test_failed_login_and_background_refresh_do_not_update_usage(self):
        first = self.select('student-0')
        self.manager.activate('student-1')
        before = dict(self.manager.accounts.last_used)
        first.services.login = lambda *args: 1
        first.login(False)
        self.drain(lambda: not first._task)
        self.assertEqual(self.manager.accounts.last_used, before)
        self.assertEqual(self.manager.accountRows[0]['username'], 'student-1')

    def test_usage_write_failure_keeps_selected_account_and_other_saved_credentials(self):
        before = self.manager.accounts.path.read_bytes()
        with patch.object(self.manager.accounts, 'remember_use', side_effect=PermissionError()):
            self.manager.activate('student-0')
        self.assertEqual(self.manager.active.account_username, 'student-0')
        self.assertEqual(self.manager.accountRows[0]['username'], 'student-0')
        self.assertIn('最近使用记录未保存', self.manager.active._state['feedback'])
        self.assertEqual(self.manager.accounts.path.read_bytes(), before)


class WorkerIsolationTests(unittest.TestCase):
    def test_stopping_one_real_worker_exits_and_cleans_its_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lines = []
            service = ProcessServices(root, log=lines.append)
            service.credentials = {'skl_username': 'account-a', 'skl_password': 'fixture'}
            write_json(root / 'live_session.json', {'username': 'account-a'})
            stop = threading.Event()
            result = []
            thread = threading.Thread(target=lambda: result.append(
                service.run('isolation-check', {}, stop, hold=True)))
            thread.start()
            try:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if (service.job_directory and (service.job_directory / 'ready').exists()
                            and any('自检已就绪' in line for line in lines)):
                        break
                    time.sleep(.02)
                else:
                    self.fail('Worker startup log was not delivered before ASR or process exit')
                self.assertTrue(thread.is_alive())
            finally:
                stop.set()
                thread.join(15)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result, [None])
            self.assertIsNone(service.process)

    def test_five_real_child_processes_use_independent_roots_and_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            processes = [ProcessServices(root / str(i)) for i in range(5)]
            results, errors = {}, []
            before = os.environ.get(SESSION_ENV)
            def run(index):
                service = processes[index]
                service.root.mkdir()
                service.credentials = {'skl_username': 'student-' + str(index),
                                       'skl_password': 'fixture-password-' + str(index)}
                write_json(service.root / 'live_session.json', {'username': service.credentials['skl_username']})
                try:
                    results[index] = service.run('isolation-check', {}, threading.Event())
                except Exception as exc:
                    errors.append(type(exc).__name__ + ': ' + str(exc))
            threads = [threading.Thread(target=run, args=(i,)) for i in range(5)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(40)
            self.assertFalse(errors, errors)
            self.assertEqual(len(results), 5)
            self.assertEqual(len({v['pid'] for v in results.values()}), 5)
            for i, result in results.items():
                self.assertEqual(result['username'], 'student-' + str(i))
                self.assertEqual(result['session_username'], result['username'])
                self.assertEqual(Path(result['root']), processes[i].root)
                for key in ('browser_root', 'live_root'):
                    self.assertEqual(Path(result[key]), processes[i].root)
                self.assertTrue(Path(result['legacy_profile']).is_relative_to(processes[i].root))
            self.assertEqual(os.environ.get(SESSION_ENV), before)

    def test_mismatched_session_is_rejected_before_course_requests(self):
        from hdusign.live import LiveError
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = ProcessServices(root)
            service.credentials = {'skl_username': 'account-a', 'skl_password': 'fixture'}
            write_json(root / 'live_session.json', {'username': 'account-b'})
            with self.assertRaisesRegex(LiveError, '不一致'):
                service.courses({}, threading.Event())


class WebhookResponseTests(unittest.TestCase):
    def test_http_success_without_business_success_does_not_pass(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        alerter = Alerter(webhook_url='https://example.invalid/hook')
        for body, expected in (({'errcode': 0}, True), ({'errcode': 40001}, False),
                               ({'errcode': False}, False), ({}, False), ([], False)):
            response.read.return_value = json.dumps(body).encode()
            with self.subTest(body=body), patch('urllib.request.urlopen', return_value=response):
                self.assertEqual(alerter._send_wechat('10:00', 'test', '测试', '测试'), expected)


if __name__ == '__main__':
    unittest.main()
