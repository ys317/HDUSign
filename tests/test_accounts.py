"""Account persistence and deletion use isolated local fixtures, no school calls."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hdusign.accounts import AccountStore, load_credentials, protect, session_credentials, SESSION_ENV
from hdusign.desktop_controller import Controller
from hdusign.demo import DemoServices
import test_desktop_controller as fixtures


class AccountTests(unittest.TestCase):
    setUp = fixtures.DesktopControllerTests.setUp
    tearDown = fixtures.DesktopControllerTests.tearDown
    complete = fixtures.DesktopControllerTests.complete

    def test_success_remembers_and_restart_fills_without_login(self):
        self.c.services.login = lambda *args: 0
        self.c.login(False)
        self.complete()
        username = self.c._form['username']
        password = self.c._form['password']
        self.assertEqual(self.c.savedAccounts, [username])
        self.assertEqual(self.c.account_store.password(username), password)
        text = self.c.account_store.path.read_text('utf-8')
        self.assertNotIn('"' + password + '"', text)
        with patch.object(DemoServices, 'login', side_effect=AssertionError('unexpected login')):
            other = Controller(self.root, DemoServices(), auto_load=False)
            other.shutdown()
            self.assertEqual(other._form['username'], username)
            self.assertEqual(other._form['password'], password)

    def test_failed_edit_does_not_replace_verified_password(self):
        username = self.c._form['username']
        self.c.account_store.save(username, 'original-good-password')
        self.c.setField('password', 'wrong-new-password')
        self.c.services.login = lambda *args: 1
        self.c.login(False)
        self.complete()
        self.assertEqual(AccountStore(self.root).password(username), 'original-good-password')
        self.assertNotIn('wrong-new-password', self.c.secrets_path.read_text('utf-8'))

    def test_unremembered_success_has_only_session_credentials(self):
        self.c.setField('username', 'temporary-account')
        self.c.setField('password', 'temporary-password')
        self.c.setField('remember', False)
        self.c.services.login = lambda *args: 0
        self.c.login(False)
        self.complete()
        self.assertNotIn('temporary-account', self.c.savedAccounts)
        self.assertEqual(load_credentials(self.root)['skl_password'], 'temporary-password')
        after_restart = load_credentials(self.root, use_session=False)
        self.assertFalse(after_restart.get('skl_password'))
        self.assertNotIn('temporary-password', self.c.secrets_path.read_text('utf-8'))

    def test_select_and_delete_clear_matching_sessions_only(self):
        store = self.c.account_store
        store.save('account-a', 'password-a')
        store.save('account-b', 'password-b')
        self.c.selectAccount('account-a')
        self.assertEqual(self.c._form['password'], 'password-a')
        paths = {}
        for user in ('account-a', 'account-b'):
            paths[user] = self.root / 'browser_profile/accounts' / hashlib.sha256(user.encode()).hexdigest()
            paths[user].mkdir(parents=True)
            (paths[user] / 'cookies').write_text('fixture')
        session = self.root / 'live_session.json'
        session.write_text(json.dumps({'username': 'account-a', 'cookies': []}))
        self.c.deleteAccount('account-a')
        self.assertEqual(self.c.savedAccounts, ['account-b'])
        self.assertFalse(paths['account-a'].exists())
        self.assertTrue(paths['account-b'].exists())
        self.assertFalse(session.exists())
        self.assertEqual(self.c._form['password'], '')
        self.assertTrue(self.c._state['needsLogin'])

    def test_locked_profile_does_not_claim_deletion(self):
        self.c.account_store.save('account-a', 'password-a')
        with patch('hdusign.accounts.remove_profile', side_effect=PermissionError()):
            self.c.deleteAccount('account-a')
        self.assertIn('account-a', self.c.savedAccounts)
        self.assertTrue(self.c._state['feedbackError'])

    def test_password_update_requires_success(self):
        username = self.c._form['username']
        self.c.account_store.save(username, 'old-password')
        self.c.setField('password', 'new-password')
        self.c.save()
        self.assertEqual(self.c.account_store.password(username), 'old-password')
        self.c.services.login = lambda *args: 0
        self.c.login(False)
        self.complete()
        self.assertEqual(self.c.account_store.password(username), 'new-password')


class EncryptionTests(unittest.TestCase):
    def test_dpapi_round_trip_unicode_and_spaces(self):
        secret = ' 密码 with spaces 123 '
        encrypted = protect(secret)
        self.assertNotIn(secret, encrypted)
        self.assertEqual(protect(encrypted, True), secret)

    def test_session_is_scoped_to_application_directory(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            with patch.dict(os.environ):
                session_credentials(first, {'skl_username': 'one', 'skl_password': 'secret'})
                self.assertNotIn('secret', os.environ[SESSION_ENV])
                self.assertEqual(load_credentials(first)['skl_username'], 'one')
                self.assertNotIn('skl_username', load_credentials(second))


class AccountUsageTests(unittest.TestCase):
    def test_legacy_vault_keeps_order_and_usage_preserves_encrypted_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            records = {'older': protect('fixture-one'), 'newer': protect('fixture-two')}
            path = root / 'accounts.json'
            path.write_text(json.dumps({'accounts': records, 'last': 'newer'}), 'utf-8')
            store = AccountStore(root)
            self.assertEqual(store.last_used, {})
            store.remember_use('older', 100)
            reloaded = AccountStore(root)
            self.assertEqual(list(reloaded.accounts), ['older', 'newer'])
            self.assertEqual(reloaded.accounts, records)
            self.assertEqual(reloaded.last, 'newer')
            self.assertEqual(reloaded.last_used, {'older': 100})
            store.save('newer', 'fixture-updated')
            store.clear_last()
            self.assertEqual(AccountStore(root).last_used, {'older': 100})
            self.assertEqual(AccountStore(root).last, '')
            store.delete('older')
            self.assertEqual(AccountStore(root).last_used, {})
            self.assertEqual(AccountStore(root).password('newer'), 'fixture-updated')

    def test_invalid_usage_metadata_does_not_block_saved_accounts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = AccountStore(root)
            for name in ('valid', 'boolean', 'text', 'negative', 'infinite'):
                store.save(name, 'fixture-password')
            value = json.loads(store.path.read_text('utf-8'))
            value['last_used'] = {'valid': 100, 'boolean': True, 'text': '200',
                                  'negative': -1, 'infinite': float('inf'), 'deleted': 300}
            store.path.write_text(json.dumps(value), 'utf-8')
            self.assertEqual(AccountStore(root).last_used, {'valid': 100})
            value['last_used'] = []
            store.path.write_text(json.dumps(value), 'utf-8')
            self.assertEqual(AccountStore(root).last_used, {})

    def test_unsaved_account_and_failed_write_do_not_change_vault(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = AccountStore(Path(temporary))
            store.save('saved', 'fixture-password')
            before = store.path.read_bytes()
            store.remember_use('temporary', 100)
            self.assertEqual(store.path.read_bytes(), before)
            with patch('hdusign.accounts.write_json', side_effect=PermissionError()):
                with self.assertRaises(PermissionError):
                    store.remember_use('saved', 200)
            self.assertEqual(store.last_used, {})
            self.assertEqual(store.path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
