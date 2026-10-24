"""HTTP auth challenges must never originate in login-detection page scripts."""
from contextlib import redirect_stdout
from io import StringIO
import json
from unittest.mock import Mock, patch
import unittest

from hdusign import live
import test_live as fixtures


class LoginAuthTests(unittest.TestCase):
    setUp = fixtures.LiveLoginTests.setUp
    run_login = fixtures.LiveLoginTests.run_login

    def test_login_route_never_reads_token_or_queries_profile_even_with_stale_token(self):
        self.page.url = fixtures.ORIGIN + '/#/login'
        self.page.wait_for_timeout.side_effect = lambda _: self.stop.set()
        import threading
        self.stop = threading.Event()
        result, _ = self.run_login(self.stop)
        self.assertEqual(result, 1)
        self.page.evaluate.assert_not_called()
        self.context.request.get.assert_not_called()

    def test_no_token_on_list_page_never_sends_a_profile_request(self):
        self.page.evaluate.return_value = {'ok': False, 'jwt_token': ''}
        import threading
        stop = threading.Event()
        self.page.wait_for_timeout.side_effect = lambda _: stop.set()
        self.assertEqual(self.run_login(stop)[0], 1)
        self.context.request.get.assert_not_called()
        self.context.cookies.assert_not_called()

    def test_401_basic_challenge_is_reported_once_without_overwriting_previous_session(self):
        import threading
        stop = threading.Event()
        stored = self.root/'live_session.json'
        stored.write_text('previous-session', 'utf-8')
        self.profile_response.status = 401
        self.profile_response.headers = {'www-authenticate': 'Basic realm="Realm"'}
        ticks = [0]
        def tick(_):
            ticks[0] += 1
            if ticks[0] == 5:
                stop.set()
        self.page.wait_for_timeout.side_effect = tick
        result, output = self.run_login(stop)
        self.assertEqual(result, 1)
        self.context.request.get.assert_called_once()
        self.profile_response.dispose.assert_called_once()
        self.assertEqual(output.count('当前登录凭据已失效'), 1)
        self.assertEqual(stored.read_text('utf-8'), 'previous-session')
        self.assertNotIn('fixture-jwt', output)

    def test_new_token_after_401_is_verified_and_account_name_comes_from_backend(self):
        denied = Mock(status=401)
        self.context.request.get.side_effect = [denied, self.profile_response]
        self.page.evaluate.return_value = {'ok': True, 'jwt_token': 'old-token', 'display_name': '页面伪造姓名'}
        self.profile_response.json.return_value = {'ok': True, 'status': 200,
                                                 'data': {'id': 1, 'userCode': 'student-a', 'userName': '真实姓名'}}
        self.page.wait_for_timeout.side_effect = lambda _: setattr(self.page.evaluate, 'return_value',
                                                                 {'ok': True, 'jwt_token': 'new-token'})
        result, _ = self.run_login()
        self.assertEqual(result, 0)
        self.assertEqual([call.kwargs['headers']['jwt-token'] for call in self.context.request.get.call_args_list],
                         ['old-token', 'new-token'])
        saved = json.loads((self.root/'live_session.json').read_text('utf-8'))
        self.assertEqual((saved['jwt_token'], saved['user_code'], saved['display_name']),
                         ('new-token', 'student-a', '真实姓名'))

    def test_profile_redirect_is_not_followed_and_never_saves_token(self):
        self.profile_response.status = 302
        self.profile_response.headers = {'location': 'https://example.invalid/steal'}
        import threading
        stop = threading.Event()
        self.page.wait_for_timeout.side_effect = lambda _: stop.set()
        self.assertEqual(self.run_login(stop)[0], 1)
        self.assertEqual(self.context.request.get.call_args.kwargs['max_redirects'], 0)
        self.assertFalse((self.root/'live_session.json').exists())

    def test_expired_business_status_stops_retrying_the_same_token(self):
        self.profile_response.json.return_value = {'ok': False, 'status': 401}
        import threading
        stop = threading.Event()
        count = [0]
        def tick(_):
            count[0] += 1
            if count[0] >= 4:
                stop.set()
        self.page.wait_for_timeout.side_effect = tick
        self.assertEqual(self.run_login(stop)[0], 1)
        self.context.request.get.assert_called_once()

    def test_token_changes_during_backend_verification_do_not_save_the_previous_identity(self):
        import threading
        stop = threading.Event()
        def profile(*args, **kwargs):
            self.page.evaluate.return_value = {'ok': True, 'jwt_token': 'new-account-token'}
            return self.profile_response
        self.context.request.get.side_effect = profile
        self.page.wait_for_timeout.side_effect = lambda _: stop.set()
        self.assertEqual(self.run_login(stop)[0], 1)
        self.context.cookies.assert_not_called()
        self.assertFalse((self.root/'live_session.json').exists())

    def test_token_changes_during_cookie_capture_do_not_save_mixed_credentials(self):
        import threading
        stop = threading.Event()
        def cookies(*_):
            self.page.evaluate.return_value = {'ok': True, 'jwt_token': 'new-account-token'}
            return [fixtures.cookie()]
        self.context.cookies.side_effect = cookies
        self.page.wait_for_timeout.side_effect = lambda _: stop.set()
        self.assertEqual(self.run_login(stop)[0], 1)
        self.assertFalse((self.root/'live_session.json').exists())

    def test_page_snapshot_does_not_implement_any_network_fetch(self):
        self.assertNotIn('fetch(', live._PROBE_LOGIN_JS)
        self.assertNotIn('XMLHttpRequest', live._PROBE_LOGIN_JS)
        self.assertNotIn('apiPath', live._PROBE_LOGIN_JS)

    def test_transient_failure_is_throttled_but_can_recover_with_same_token(self):
        transient = Mock(status=503)
        self.context.request.get.side_effect = [transient, self.profile_response]
        clock = [100.0]
        self.page.wait_for_timeout.side_effect = lambda _: clock.__setitem__(0, clock[0]+.25)
        with patch.object(live.time, 'monotonic', side_effect=lambda: clock[0]):
            result, _ = self.run_login()
        self.assertEqual(result, 0)
        self.assertEqual(self.context.request.get.call_count, 2)
        self.assertGreaterEqual(clock[0], 102)


if __name__ == '__main__':
    unittest.main()
