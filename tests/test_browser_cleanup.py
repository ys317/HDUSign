"""Profile cleanup only removes locks after a safe, read-only process check."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from hdusign import browser


@unittest.skipUnless(sys.platform == "win32", "Windows browser command-line parsing")
class CommandLineProfileTests(unittest.TestCase):
    def setUp(self):
        self.profile = Path(r"C:\EchoSign\browser_profile\accounts\abc")

    def _match(self, arguments):
        with patch.object(browser, "_windows_commandline_args", return_value=arguments):
            return browser._commandline_uses_profile("ignored", self.profile)

    def test_exact_user_data_dir_matches_equals_and_separate_forms(self):
        expected = str(self.profile)
        self.assertTrue(self._match(["chrome.exe", f"--user-data-dir={expected}"]))
        self.assertTrue(self._match(["chrome.exe", "--user-data-dir", expected]))

    def test_path_substrings_and_other_options_do_not_match(self):
        expected = str(self.profile)
        self.assertFalse(self._match(["chrome.exe", f"--proxy-server={expected}"]))
        self.assertFalse(self._match(["chrome.exe", f"--user-data-dir={expected}-other"]))
        self.assertFalse(self._match(["chrome.exe", "--user-data-dir", expected + "-other"]))

    def test_last_repeated_user_data_dir_value_is_used(self):
        expected = str(self.profile)
        self.assertTrue(self._match([
            "chrome.exe", "--user-data-dir=C:\\other-profile",
            f"--user-data-dir={expected}",
        ]))
        self.assertFalse(self._match([
            "chrome.exe", f"--user-data-dir={expected}",
            "--user-data-dir=C:\\other-profile",
        ]))

    def test_relative_user_data_dir_is_not_guessed(self):
        with patch.object(browser, "_windows_commandline_args",
                          return_value=["chrome.exe", "--user-data-dir=profile"]):
            with self.assertRaises(ValueError):
                browser._commandline_uses_profile("ignored", self.profile)


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.profile = Path(self.temp.name) / "account"
        self.profile.mkdir()
        for name in ("lockfile", "SingletonLock", "SingletonCookie", "SingletonSocket"):
            (self.profile / name).write_text("lock", encoding="utf-8")
        self.process_query = Mock(returncode=0, stdout="[]", stderr="")

    def _cleanup(self, processes):
        self.process_query.stdout = json.dumps(processes)
        with patch.object(browser.sys, "platform", "win32"), \
                patch.object(subprocess, "run", return_value=self.process_query) as run, \
                patch.object(Path, "unlink", autospec=True) as unlink:
            browser._cleanup_stale_profile(self.profile)
        return run, unlink

    @unittest.skipUnless(sys.platform == "win32", "Windows browser process cleanup")
    def test_no_matching_process_removes_only_stale_locks(self):
        run, unlink = self._cleanup([{
            "ProcessId": 22,
            "CommandLine": "chrome.exe --user-data-dir=C:\\other-profile",
        }])
        run.assert_called_once()
        self.assertEqual(unlink.call_count, 4)
        self.assertFalse(any(call.args[0] == "taskkill" for call in run.call_args_list))

    @unittest.skipUnless(sys.platform == "win32", "Windows browser process cleanup")
    def test_matching_process_is_left_running_and_locks_are_preserved(self):
        command_line = f'chrome.exe --user-data-dir="{self.profile}"'
        with patch.object(browser, "_commandline_uses_profile", return_value=True) as uses, \
                patch.object(browser.sys, "platform", "win32"), \
                patch.object(subprocess, "run", return_value=Mock(
                    returncode=0, stdout=json.dumps([{"ProcessId": 22, "CommandLine": command_line}]),
                    stderr="")
                    ) as run, \
                patch.object(Path, "unlink", autospec=True) as unlink:
            with self.assertRaisesRegex(RuntimeError, "仍在使用"):
                browser._cleanup_stale_profile(self.profile)
        uses.assert_called_once()
        run.assert_called_once()
        unlink.assert_not_called()

    @unittest.skipUnless(sys.platform == "win32", "Windows browser process cleanup")
    def test_unreadable_process_inventory_fails_closed(self):
        with patch.object(browser.sys, "platform", "win32"), \
                patch.object(subprocess, "run", return_value=Mock(
                    returncode=0, stdout=json.dumps([{"ProcessId": 22}]), stderr="")), \
                patch.object(Path, "unlink", autospec=True) as unlink:
            with self.assertRaisesRegex(RuntimeError, "无法确认"):
                browser._cleanup_stale_profile(self.profile)
        unlink.assert_not_called()

    def test_non_windows_does_not_touch_processes_or_locks(self):
        with patch.object(browser.sys, "platform", "linux"), \
                patch.object(subprocess, "run") as run, \
                patch.object(Path, "unlink", autospec=True) as unlink:
            browser._cleanup_stale_profile(self.profile)
        run.assert_not_called()
        unlink.assert_not_called()


if __name__ == "__main__":
    unittest.main()
