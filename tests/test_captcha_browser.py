"""Real Chromium against intercepted fixtures only; all network is blocked."""
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import unittest
import tempfile
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from hdusign import browser
from hdusign import captcha_boost as cb


FIXTURE = """<!doctype html><html><meta charset="utf-8"><body>
<button onclick="submitSign()">签到</button>
<div id="aliyunCaptcha-window-popup" style="display:none">请完成安全验证</div>
<script>
const challenge = document.getElementById('aliyunCaptcha-window-popup');
let clicks = 0;
async function sendCode() {
    return fetch('/api/ali-nvc/captcha-verify?code=1234', {method:'POST'});
}
async function submitSign() {
    clicks++;
    if (window.initialResponse) await sendCode();
    challenge.style.display = 'block';
    // Simulates a person completing this inert fixture. No real CAPTCHA exists.
    setTimeout(async () => {
        challenge.style.display = 'none';
        await sendCode();
    }, 600);
}
</script></body></html>"""


class ChromiumVerificationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        library = patch.object(cb, "LIBRARY_PATH", Path(temp.name) / "library.json")
        library.start()
        self.addCleanup(library.stop)
        self.addCleanup(cb._boosts.clear)
        self.addCleanup(cb._pointer_positions.clear)

    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)
        if not Path(cls.playwright.chromium.executable_path).exists():
            raise unittest.SkipTest("Local Playwright Chromium is not installed")
        cls.chromium = cls.playwright.chromium.launch(headless=True)
        cls.addClassCleanup(cls.chromium.close)

    def test_input_observer_distinguishes_our_click_and_manual_takeover(self):
        context = self.chromium.new_context(service_workers="block")
        self.addCleanup(context.close)
        context.route("**/*", lambda route: route.abort())
        page = context.new_page()
        page.set_content('<button>1</button><iframe srcdoc="<button>frame</button>"></iframe>')
        cb.install_for(page)
        boost = cb._boosts[page]
        self.assertTrue(boost._observing_inputs)
        browser.click_visible(page, "button", "1", human_path=True)
        self.assertFalse(cb.manual_for(page))
        self.assertGreater(boost._last_automatic_at, 0)
        cb.note_click_for(page, 10, 10)
        # An unwrapped trusted input represents an action outside our automation.
        page.frames[1].get_by_role("button").click()
        page.evaluate("0")
        self.assertTrue(cb.manual_for(page))
        self.assertFalse(boost._armed)
        self.assertIsNotNone(boost._manual_input_at)

    def test_click_error_cancels_rewrite_before_human_can_retry(self):
        context = self.chromium.new_context(service_workers="block")
        self.addCleanup(context.close)
        context.route("**/*", lambda route: route.abort())
        page = context.new_page()
        page.set_content('<div class="nc-container"><button>点击开始验证</button></div>')
        cb.install_for(page)
        with patch.object(type(page.locator("button")), "click",
                          side_effect=browser.PlaywrightError("fixture click failed")):
            self.assertTrue(browser._click_verification(page, lambda: None))
        self.assertTrue(cb.manual_for(page))
        self.assertFalse(cb._boosts[page]._armed)

    def test_input_observer_survives_navigation_and_new_frames(self):
        context = self.chromium.new_context(service_workers="block")
        self.addCleanup(context.close)
        context.route("**/*", lambda route: route.abort())
        page = context.new_page()
        cb.install_for(page)
        page.goto('data:text/html,<iframe srcdoc="<button>new frame</button>"></iframe>')
        page.frames[1].get_by_role("button").click()
        page.evaluate("0")
        self.assertTrue(cb.manual_for(page))

    def test_ali_button_is_clicked_once_and_success_still_requires_server_result(self):
        html = """<!doctype html><meta charset="utf-8">
        <script>let submits = 0, verificationClicks = 0;</script>
        <button onclick="submits++; document.getElementById('aliyunCaptcha-window-popup').hidden=false">签到</button>
        <div id="aliyunCaptcha-window-popup" hidden>
          <button onclick="verificationClicks++; if (window.completeVerification) {
            this.parentElement.hidden=true;
            fetch('/api/ali-nvc/captcha-verify?code=1234', {method:'POST'});
          }">点击按钮开始智能验证</button>
        </div>"""
        for completed in (True, False):
            with self.subTest(completed=completed):
                context = self.chromium.new_context(service_workers="block")
                try:
                    def route_fixture(route):
                        if route.request.url == browser.START:
                            route.fulfill(content_type="text/html", body=html)
                        elif browser._is_sign_endpoint(route.request.url, route.request.method, "1234"):
                            route.fulfill(json={"code": 200})
                        else:
                            route.abort()
                    context.route("**/*", route_fixture)
                    page = context.new_page()
                    page.goto(browser.START)
                    page.evaluate("value => window.completeVerification = value", completed)
                    stages = []
                    with redirect_stdout(StringIO()):
                        result = browser._wait_for_sign_result(
                            page, "1234", lambda name, when: stages.append(name), lambda: None, timeout_s=4.2)
                    self.assertEqual(result.status, "success" if completed else "unknown")
                    self.assertEqual(page.evaluate("[submits, verificationClicks]"), [1, 1])
                    self.assertEqual(stages.count("verification_clicked"), 1)
                    self.assertEqual(stages.count("verification_required"), 0 if completed else 1)
                finally:
                    context.close()

    def test_only_explicit_unchecked_verification_controls_are_clicked(self):
        cases = [
            ('<div class="nc-container"><label><input type="checkbox">我不是机器人</label></div>', True),
            ('<div class="nc-container"><label><input type="checkbox" checked>我不是机器人</label></div>', False),
            ('<div class="nc-container"><button disabled>点击开始验证</button></div>', False),
            ('<div class="nc-container"><button>请按住滑块，拖动到最右边</button></div>', False),
            ('<button>点击开始验证</button>', False),
        ]
        context = self.chromium.new_context(service_workers="block")
        try:
            context.route("**/*", lambda route: route.abort())
            page = context.new_page()
            for html, expected in cases:
                with self.subTest(html=html):
                    page.set_content(html + '<script>window.clicks=0;document.onclick=()=>window.clicks++;</script>')
                    self.assertEqual(browser._click_verification(page, lambda: None), expected)
                    self.assertEqual(page.evaluate("window.clicks > 0"), expected)
                    if ' checked' in html:
                        self.assertTrue(page.locator('input').is_checked())
        finally:
            context.close()

    def test_visible_iframe_control_is_supported_but_hidden_iframe_is_ignored(self):
        context = self.chromium.new_context(service_workers="block")
        try:
            context.route("**/*", lambda route: route.abort())
            page = context.new_page()
            page.set_content("""<iframe srcdoc='<div class="nc-container">
                <button onclick="window.clicked=true">点击按钮进行验证</button></div>'></iframe>""")
            frame = page.frames[1]
            frame.get_by_role("button").wait_for()
            self.assertTrue(browser._click_verification(page, lambda: None))
            self.assertTrue(frame.evaluate("window.clicked"))
            frame.evaluate("window.clicked=false")
            page.locator("iframe").evaluate("el => el.hidden = true")
            self.assertFalse(browser._click_verification(page, lambda: None))
            self.assertFalse(frame.evaluate("window.clicked"))
        finally:
            context.close()

    def test_captcha_visibility_requires_layout_and_visible_ancestors(self):
        context = self.chromium.new_context(service_workers="block")
        try:
            context.route("**/*", lambda route: route.abort())
            page = context.new_page()
            cases = [
                ('<iframe style="display:none" src="https://captcha.example/background"></iframe>', False),
                ('<iframe title="安全验证码" src="about:blank"></iframe>', True),
                ('<div style="display:none"><iframe title="验证码"></iframe></div>', False),
                ('<div style="opacity:0"><div role="dialog">安全验证</div></div>', False),
                ('<div style="visibility:hidden"><div role="dialog">安全验证</div></div>', False),
                ('<div aria-hidden="true"><div role="dialog">安全验证</div></div>', False),
                ('<div style="width:0;height:0;overflow:hidden"><div role="dialog" '
                 'style="width:240px;height:120px">安全验证</div></div>', False),
                ('<div role="dialog" style="width:240px;height:120px">安全验证</div>', True),
                ('<div id="aliyunCaptcha-window-popup" style="width:0;height:0"></div>', False),
            ]
            for html, expected in cases:
                with self.subTest(html=html):
                    page.set_content(html)
                    self.assertEqual(browser._captcha_visible(page), expected)
        finally:
            context.close()

    def test_prewarmed_page_health_check_does_not_refresh_or_submit(self):
        context = self.chromium.new_context(service_workers="block")
        try:
            requests = []
            def fixture(route):
                requests.append(route.request.url)
                route.fulfill(content_type="text/html", body='''<meta charset="utf-8">
                    <button id="zero">0</button><button id="submit" disabled>签到</button>
                    <script>window.clicks=0;document.onclick=()=>window.clicks++;</script>''')
            context.route("**/*", fixture)
            page = context.new_page()
            page.goto(browser.START + "#/sign/in")
            self.assertFalse(browser.prepared_page_ready(page))
            page.evaluate("localStorage.setItem('sessionId', 'fixture-token')")
            # The submit button is legitimately disabled on an empty keypad.
            self.assertTrue(browser.prepared_page_ready(page))
            page.locator("#zero").evaluate("el => el.disabled = true")
            self.assertFalse(browser.prepared_page_ready(page))
            page.locator("#zero").evaluate("el => el.disabled = false")
            page.locator("#submit").evaluate("el => el.hidden = true")
            self.assertFalse(browser.prepared_page_ready(page))
            page.locator("#submit").evaluate("el => el.hidden = false")
            page.evaluate("location.hash = '#/other'")
            self.assertFalse(browser.prepared_page_ready(page))
            self.assertEqual(page.evaluate("window.clicks"), 0)
            self.assertEqual(len(requests), 1)
            page.goto("https://example.com/#/sign/in")
            page.evaluate("localStorage.setItem('sessionId', 'fixture-token')")
            self.assertFalse(browser.prepared_page_ready(page))
        finally:
            context.close()

    def test_widget_and_intermediate_response_continue_to_attendance_result(self):
        for initial in (False, True):
            with self.subTest(initial_response=initial):
                context = self.chromium.new_context(service_workers="block")
                try:
                    payloads = ([{"code": 400, "msg": "请完成验证码"}] if initial else []) + [{"code": 200}]
                    requests = []
                    def fixture(route):
                        url = route.request.url
                        requests.append(url)
                        if url == browser.START:
                            route.fulfill(content_type="text/html", body=FIXTURE)
                        elif url == "https://skl.hdu.edu.cn/api/ali-nvc/captcha-verify?code=1234":
                            route.fulfill(json=payloads.pop(0))
                        else:
                            route.abort()
                    context.route("**/*", fixture)
                    page = context.new_page()
                    page.goto(browser.START)
                    self.assertFalse(browser._captcha_visible(page))
                    page.evaluate("initial => window.initialResponse = initial", initial)
                    stages = []
                    with redirect_stdout(StringIO()):
                        result = browser._wait_for_sign_result(
                            page, "1234", lambda name, when: stages.append(name), lambda: None, timeout_s=5)
                    self.assertEqual(result.status, "success")
                    self.assertEqual(page.evaluate("clicks"), 1)
                    self.assertEqual(stages.count("verification_required"), 1)
                    self.assertEqual(stages[-1], "response")
                    self.assertFalse(browser._captcha_visible(page))
                    self.assertEqual(payloads, [])
                    self.assertEqual(len(requests), 3 if initial else 2)
                finally:
                    context.close()


if __name__ == "__main__":
    unittest.main()
