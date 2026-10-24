"""Notification identity and course snapshots; no webhook or school writes."""
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from hdusign.alert import Alerter
from hdusign import live, monitor
import test_live as live_fixtures
import test_desktop_controller as desktop_fixtures


class NotificationContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.saved = {"origin": live_fixtures.ORIGIN, "username": "student-a",
                      "jwt_token": "fixture-jwt", "cookies": []}
        self.cfg = {"live_url": live_fixtures.PAGE,
                    "notification_context": {"username": "student-a", "course_name": "公司金融学"}}
        self.root_patch = patch.object(live, 'application_root', return_value=self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def write_session(self):
        (self.root / 'live_session.json').write_text(json.dumps(self.saved), 'utf-8')

    def test_verified_cached_name_is_available_without_network(self):
        self.saved.update(display_name='张同学', user_code='student-a')
        self.write_session()
        with patch.object(live, '_authenticated_session', side_effect=AssertionError('no network')):
            self.assertEqual(live.notification_name(self.cfg), '张同学')

    def test_previous_accounts_name_is_never_used(self):
        self.saved.update(username='student-b', display_name='其他同学', user_code='student-b')
        self.write_session()
        with patch.object(live, '_authenticated_session', side_effect=AssertionError('no network')):
            self.assertEqual(live.notification_name(self.cfg), '')

    def test_old_session_resolves_official_user_name_and_checks_user_code(self):
        self.write_session()
        session = Mock()
        session.get.return_value.status_code = 200
        payload = {"ok": True, "status": 200,
                   "data": {"id": 1, "userCode": "student-a", "userName": "张同学"}}
        session.get.return_value.json.return_value = payload
        with patch.object(live, '_authenticated_session', return_value=(session, [])):
            self.assertEqual(live.notification_name(self.cfg), '张同学')
            payload['data']['userCode'] = 'student-b'
            self.assertEqual(live.notification_name(self.cfg), '')
        self.assertEqual(session.close.call_count, 2)
        self.assertTrue(session.get.call_args.args[0].endswith('/v1/currentuser'))
        self.assertFalse(session.get.call_args.kwargs['allow_redirects'])

    def test_network_failure_leaves_notification_available(self):
        self.write_session()
        with patch.object(live, '_authenticated_session', side_effect=live.LiveLoginRequired('expired')):
            self.assertEqual(live.notification_name(self.cfg), '')

    def test_all_notification_levels_include_name_and_course(self):
        alerter = Alerter(webhook_url='https://example.invalid/hook',
                          student_name='张同学', course_name='公司金融学')
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"errcode":0}'
        with patch('urllib.request.urlopen', return_value=response) as send:
            for level in ('high', 'code', 'test'):
                alerter._send_wechat('10:00:00', level, '测试原因', '签到码 1234')
                body = json.loads(send.call_args.args[0].data)['markdown']['content']
                self.assertIn('姓名：张同学', body)
                self.assertIn('课程：公司金融学', body)
                self.assertIn('签到码 1234', body)

    def test_missing_name_is_explicit_and_markdown_is_escaped(self):
        alerter = Alerter(webhook_url='https://example.invalid/hook', course_name='课程*一*\n伪造行')
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"errcode":0}'
        with patch('urllib.request.urlopen', return_value=response) as send:
            alerter._send_wechat('10:00:00', 'code', '测试', '1234')
        body = json.loads(send.call_args.args[0].data)['markdown']['content']
        self.assertIn('姓名：未获取，请重新登录', body)
        self.assertIn('课程\\*一\\* 伪造行', body)

    def test_make_alerter_preserves_context_for_code_notification(self):
        self.cfg['alert'] = {'webhook': {'url': 'https://example.invalid/hook'}}
        with patch.object(live, 'notification_name', return_value='张同学'):
            alerter = monitor.make_alerter(self.cfg)
        self.assertEqual((alerter.student_name, alerter.course_name), ('张同学', '公司金融学'))


class LoginProfileTests(unittest.TestCase):
    def test_successful_login_records_name_from_verified_profile(self):
        fixture = live_fixtures.LiveLoginTests()
        fixture.setUp()
        try:
            fixture.page.evaluate.return_value = {'ok': True, 'jwt_token': 'fixture-jwt',
                                                  'display_name': '张同学', 'user_code': 'student-a'}
            fixture.profile_response.json.return_value = {'ok': True, 'status': 200, 'data': {
                'id': 1, 'userName': '张同学', 'userCode': 'student-a'}}
            with redirect_stdout(StringIO()):
                live.login_live(live_fixtures.PAGE,
                                credentials={'skl_username': 'student-a', 'skl_password': 'secret'})
            saved = json.loads((fixture.root / 'live_session.json').read_text('utf-8'))
            self.assertEqual((saved['display_name'], saved['user_code']), ('张同学', 'student-a'))
            self.assertNotIn('secret', json.dumps(saved))
        finally:
            fixture.doCleanups()


class ControllerContextTests(unittest.TestCase):
    setUp = desktop_fixtures.DesktopControllerTests.setUp
    tearDown = desktop_fixtures.DesktopControllerTests.tearDown
    complete = desktop_fixtures.DesktopControllerTests.complete
    select = desktop_fixtures.DesktopControllerTests.select

    def test_monitor_uses_selected_course_and_account_snapshot(self):
        selected = self.select()
        captured = []
        self.c.services.monitor = lambda cfg, stop: captured.append(cfg)
        self.c.start_monitor()
        self.complete()
        self.assertEqual(captured[0]['notification_context'],
                         {'username': self.c._form['username'], 'course_name': selected.title})
        self.assertNotIn('notification_context', self.c.cfg)


if __name__ == '__main__':
    unittest.main()
