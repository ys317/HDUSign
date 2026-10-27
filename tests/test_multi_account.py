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

    def signin(self, cfg, stop):
        self.calls.append(copy.deepcopy(self.credentials))
        return 0

    def locate(self, cfg, stop):
        from hdusign.location import Location
        return Location(30.123456, 120.654321, 5)


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

    def test_legacy_loopback_setting_can_start_in_multi_account_desktop(self):
        controller = self.manager.controllers['student-0']
        cfg = yaml.safe_load(controller.config_path.read_text('utf-8'))
        cfg['live_audio']['enabled'] = False
        cfg['live_url'] = live_course_url(demo_courses()[0])
        controller.config_path.write_text(yaml.safe_dump(cfg), encoding='utf-8')
        other = AccountManager(self.root, services_factory=IsolatedFakeServices, auto_load=False)
        restored = other.controllers['student-0']
        try:
            self.assertTrue(restored._form['live'])
            self.assertTrue(restored.cfg['live_audio']['enabled'])
            # Do not select a course: selection itself enables live audio and
            # would conceal a broken migration of the old disabled setting.
            self.assertTrue(restored.start_monitor())
            self.assertTrue(yaml.safe_load(restored.config_path.read_text('utf-8'))['live_audio']['enabled'])
        finally:
            other.shutdown()
            if restored.worker:
                restored.worker.join(2)
            restored.drain()

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

    def test_signin_from_new_account_uses_its_own_controller_and_credentials(self):
        self.manager.activate('')
        draft = self.manager.active
        draft.setField('username', 'signin-student')
        draft.setField('password', 'signin-password')
        draft.runAction('signin')
        target = self.manager.active
        self.drain(lambda: not target._task)
        self.assertEqual(target.account_username, 'signin-student')
        self.assertEqual(target.root, self.manager.account_root('signin-student'))
        self.assertNotEqual(target.root, draft.root)
        self.assertEqual(target.services.calls, [{'skl_username': 'signin-student', 'skl_password': 'signin-password'}])

    def test_second_save_write_failure_preserves_worker_credentials_and_reservations(self):
        from hdusign.desktop_controller import write_text_atomic
        controller = self.select('student-0', True)
        previous_cfg = controller.config_path.read_bytes()
        previous_credentials = dict(controller.services.credentials)
        controller._form['password'] = 'uncommitted-edit'
        writes = []

        def fail_second(path, text):
            writes.append(path)
            if len(writes) == 2:
                raise PermissionError('second save write failed')
            write_text_atomic(path, text)

        with patch('hdusign.desktop_controller.write_text_atomic', side_effect=fail_second):
            controller.toggleMonitor()
        self.assertEqual(controller.config_path.read_bytes(), previous_cfg)
        self.assertEqual(controller.services.credentials, previous_credentials)
        self.assertFalse(controller._schedules)
        other = AccountManager(self.root, services_factory=IsolatedFakeServices, auto_load=False)
        try:
            self.assertFalse(other.controllers['student-0']._schedules)
        finally:
            other.shutdown()

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


class AccountActionProtocolTests(unittest.TestCase):
    def test_signin_and_location_actions_are_serializable_without_live_login(self):
        from hdusign.account_worker import worker_main
        from hdusign.accounts import load_credentials
        from hdusign.location import Location
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = ProcessServices(root)
            credentials = {'skl_username': 'action-student', 'skl_password': 'action-password'}
            environment = service.environment(credentials)
            for action in ('signin', 'locate'):
                with self.subTest(action=action):
                    write_json(root / 'request.json', dict(action=action, root=str(root),
                               parent_pid=os.getpid(), config={'auto_sign': {'enabled': True}}))
                    captured = []

                    def fake_action(cfg, stop):
                        captured.append(load_credentials(root))
                        return Location(30.1, 120.2, 5) if action == 'locate' else 0

                    with patch.dict(os.environ, environment, clear=True), \
                            patch('hdusign.account_worker.os.chdir'), \
                            patch('hdusign.desktop_controller.Services.' + action, side_effect=fake_action):
                        self.assertEqual(worker_main([str(root)]), 0)
                    self.assertEqual(captured, [credentials])
                    result = read_json(root / 'result.json')
                    self.assertTrue(result['ok'])
                    self.assertEqual(result['value'], {'lat': 30.1, 'lng': 120.2, 'accuracy_m': 5}
                                     if action == 'locate' else 0)
            self.assertFalse((root / 'live_session.json').exists())


if __name__ == '__main__':
    unittest.main()
