"""Read-only anonymous check: no saved accounts, passwords or sign-in submits."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import portable_tests
from playwright.sync_api import sync_playwright
from hdusign.live import _login_candidate, _verify_browser_login, _block_viewing_writes, _API

report_path = Path(sys.argv[1]).resolve()
report = {'ok': False, 'credentials_used': False, 'sign_in_submitted': False}
with sync_playwright() as pw:
    executable = Path(__file__).resolve().parents[2]/'_internal/browsers/chromium-1217/chrome-win64/chrome.exe'
    browser = pw.chromium.launch(headless=True, executable_path=str(executable))
    context = browser.new_context()
    context.route('**/*', _block_viewing_writes)
    page = context.new_page()
    browser_profile_requests = []
    page.on('request', lambda request: browser_profile_requests.append(request) if '/v1/currentuser' in request.url else None)
    try:
        response = page.goto('https://course.hdu.edu.cn/#/list-live', wait_until='domcontentloaded', timeout=15000)
        page.wait_for_timeout(1200)
        before = len(browser_profile_requests)
        for _ in range(5):
            assert not _login_candidate(page, 'https://course.hdu.edu.cn')
        assert len(browser_profile_requests) == before
        class ObservedAPI:
            def get(self, *args, **kwargs):
                response = context.request.get(*args, **kwargs)
                report['backend_status'] = response.status
                report['backend_basic_challenge'] = response.headers.get('www-authenticate', '').lower().startswith('basic')
                return response
        result = _verify_browser_login(SimpleNamespace(request=ObservedAPI()), 'https://course.hdu.edu.cn', '')
        assert result.get('ok') is False
        assert len(browser_profile_requests) == before
        report.update(ok=True, document_status=response.status,
                      anonymous_candidate_empty=True, anonymous_detection_added_requests=0,
                      backend_error_handled=True, backend_verification_added_browser_requests=0)
    finally:
        context.close()
        browser.close()
report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
print(json.dumps(report, indent=2))
