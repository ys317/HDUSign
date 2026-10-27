"""Sensitive input is opt-in in operation diagnostics; all sign-in I/O is mocked."""
from contextlib import redirect_stdout
from io import StringIO
import os
import unittest
from unittest.mock import Mock, patch

from hdusign import browser
from hdusign.attendance import AutoSigner, SignResult, diagnostic_sign_code


class SignDiagnosticsTests(unittest.TestCase):
    def test_code_logging_requires_explicit_opt_in(self):
        for value, expected in (("", "****"), ("0", "****"), ("true", "****"), ("1", "1234")):
            with self.subTest(value=value), patch.dict(os.environ, {"ECHOSIGN_DEBUG_SIGN_INPUT": value}):
                self.assertEqual(diagnostic_sign_code("1234"), expected)

    def test_browser_does_not_read_or_print_page_fields(self):
        page = Mock()
        with patch.dict(os.environ, {"ECHOSIGN_DEBUG_SIGN_INPUT": "0"}), \
                patch.object(browser, "open_sign_in"), \
                patch.object(browser, "click_visible"), \
                patch.object(browser, "_wait_for_sign_result", return_value=SignResult("success", "1234", "OK")), \
                redirect_stdout(StringIO()) as output:
            browser.sign_with_code(None, page, "1234")
        self.assertNotIn("1234", output.getvalue())
        page.query_selector_all.assert_not_called()

    def test_worker_diagnostics_mask_code_while_notifications_still_identify_request(self):
        notify = Mock()
        signer = AutoSigner(notify)
        signer._session = Mock()
        signer._session.submit.return_value = SignResult("success", "1234", "OK")
        with patch.dict(os.environ, {"ECHOSIGN_DEBUG_SIGN_INPUT": "0"}), redirect_stdout(StringIO()) as output:
            signer._sign_one("1234")
        self.assertNotIn("1234", output.getvalue())
        self.assertIn("****", notify.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
