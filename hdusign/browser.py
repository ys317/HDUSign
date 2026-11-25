"""Browser login and attendance workflows for HDU's 上课啦."""
from __future__ import annotations

import argparse
from collections import deque
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import sys
import time
from urllib.parse import parse_qs, urlparse

import yaml
from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError, sync_playwright

from hdusign import captcha_boost
from hdusign.attendance import SignResult, classify_response, diagnostic_sign_code, write_json
from hdusign.location import DEFAULT_LAT, DEFAULT_LNG
from hdusign.processes import hidden_subprocess_options
from hdusign.runtime import (
    data_root as application_root,
    configure_browser_runtime,
    external_subprocess_kwargs,
)

ROOT = application_root()
PROFILE = ROOT / "browser_profile"
START = "https://skl.hdu.edu.cn/index.html"
_ATTENDANCE_HOST = "skl.hdu.edu.cn"
_ATTENDANCE_SSO_HOSTS = frozenset(("sso.hdu.edu.cn", "cas.hdu.edu.cn"))
_SSO_HOSTS = _ATTENDANCE_SSO_HOSTS | frozenset((
    "https-sso-hdu-edu-cn-443.webvpn.hdu.edu.cn",
    "https-cas-hdu-edu-cn-443.webvpn.hdu.edu.cn",
))
_EDITABLE_INPUT = ":visible:enabled:not([readonly])"
_USERNAME_INPUT = (
    "input:is([name=username], #username, [name=account], [autocomplete=username], "
    "[placeholder*=账号], [placeholder*=学号], [placeholder*=用户名])" + _EDITABLE_INPUT
)
_USERNAME_FALLBACK = "input[type=text]:not([name*=ver])" + _EDITABLE_INPUT
_PASSWORD_INPUT = "input:is([type=password], #password, [name=password], [autocomplete=current-password])"


class SignPreparationError(RuntimeError):
    """The login or sign-in page was unavailable before any code was entered."""


def _https_host(url: str) -> str | None:
    try:
        parsed = urlparse(url)
        if parsed.scheme == "https" and parsed.port in (None, 443):
            return parsed.hostname
    except ValueError:
        pass
    return None


def _attendance_session_id(page) -> str:
    return page.evaluate("""() => location.origin === 'https://skl.hdu.edu.cn'
        ? localStorage.getItem('sessionId') || '' : ''""")


def account_profile(secrets: dict) -> Path:
    """Keep attendance sessions tied to the account entered on the home page."""
    username = str(secrets.get("skl_username", "")).strip()
    if not username:
        return PROFILE
    identity = hashlib.sha256(username.encode("utf-8")).hexdigest()
    return PROFILE / "accounts" / identity


def load_secrets() -> dict:
    from hdusign.accounts import load_credentials
    return load_credentials(ROOT)


def try_sso_login(page, secrets: dict) -> bool:
    """Fill the visible HDU SSO fields and attempt one submit."""
    host = _https_host(page.url)
    if host not in _SSO_HOSTS:
        return False
    user = secrets.get("skl_username")
    password = secrets.get("skl_password")
    if not user or not password:
        print("[!] 缺少学号或密码，请在浏览器中完成登录，或在 EchoSign 中重新填写账号密码。", flush=True)
        return False

    def fill(field, value):
        # Pin the field to this document so an operation cannot follow a redirect.
        element = field.element_handle(timeout=5000)
        if _https_host(page.url) != host:
            return False
        element.click(timeout=2000)
        element.fill(value, timeout=2000)
        return True

    try:
        available_user = page.locator(f"{_USERNAME_INPUT}, {_USERNAME_FALLBACK}").first
        available_user.wait_for(state="visible", timeout=5000)
        preferred_user = page.locator(_USERNAME_INPUT).first
        if not fill(preferred_user if preferred_user.is_visible() else available_user, user):
            return False
        password_field = page.locator(_PASSWORD_INPUT + _EDITABLE_INPUT).first
        if not fill(password_field, password):
            return False
    except PlaywrightError:
        print("[!] 未能填齐学号和密码，请在浏览器中完成登录。", flush=True)
        return False

    print("[i] 已填写学号和密码，如需验证请在浏览器中完成。", flush=True)
    if _https_host(page.url) != host:
        return False
    try:
        for selector in ("button.login-button", "button:has-text('登 录')", "button:has-text('登录')",
                         "#login_submit", "input[type=submit]", "button[type=submit]", ".login-btn", "#loginBtn"):
            button = page.locator(selector).filter(visible=True).first
            if button.is_visible():
                button = page.locator(f"{selector}:not(.disabled)").filter(visible=True).first
                button.click(timeout=8000)
                return True
        page.keyboard.press("Enter")
        return True
    except PlaywrightError:
        print("[!] 未能提交登录表单，请在浏览器中完成登录。", flush=True)
        return False


CAMPUS_HOSTS = ("skl.hdu.edu.cn", "sso.hdu.edu.cn", "cas.hdu.edu.cn")


def _browser_args() -> list[str]:
    """Optional network workarounds from config.yaml `browser:`.

    ipv4_only pins campus hosts to their IPv4 addresses (some networks close
    IPv6 TLS to hdu.edu.cn); bypass_proxy ignores a system proxy such as Clash
    that breaks TLS to those hosts. Both default to off.
    """
    options = {}
    cfgp = ROOT / "config.yaml"
    if cfgp.exists():
        try:
            options = (yaml.safe_load(cfgp.read_text(encoding="utf-8")) or {}).get("browser") or {}
        except Exception:  # noqa: BLE001
            options = {}
    args: list[str] = []
    if options.get("bypass_proxy"):
        args.append("--no-proxy-server")
    if options.get("ipv4_only"):
        import socket

        rules = []
        for host in CAMPUS_HOSTS:
            try:
                addresses = sorted({a[4][0] for a in socket.getaddrinfo(host, 443, socket.AF_INET)})
            except socket.gaierror:
                continue
            if addresses:
                rules.append(f"MAP {host} {addresses[0]}")
        if rules:
            args.append("--host-resolver-rules=" + ", ".join(rules))
    return args


def _windows_commandline_args(command_line: str) -> list[str]:
    """Use Windows' quoting rules, including quoted values after an equals sign."""
    import ctypes
    from ctypes import wintypes

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    parse = shell32.CommandLineToArgvW
    parse.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int))
    parse.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel32.LocalFree.restype = ctypes.c_void_p
    count = ctypes.c_int()
    arguments = parse(command_line, ctypes.byref(count))
    if not arguments:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return [arguments[index] for index in range(count.value)]
    finally:
        kernel32.LocalFree(ctypes.cast(arguments, ctypes.c_void_p))


def _commandline_uses_profile(command_line: str, profile: Path) -> bool:
    """Match a parsed user-data-dir value, never a path substring in another arg."""
    arguments = _windows_commandline_args(command_line)
    expected = os.path.normcase(str(profile.resolve()))
    values = []
    for index, argument in enumerate(arguments[1:], 1):
        option, separator, value = argument.partition("=")
        if option.lower() != "--user-data-dir":
            continue
        if not separator:
            value = arguments[index + 1] if index + 1 < len(arguments) else ""
        # A relative path belongs to the process's working directory, which CIM
        # does not provide. Do not guess and then delete potentially live locks.
        if not value or not Path(value).is_absolute():
            raise ValueError("无法确认浏览器使用的用户数据目录")
        values.append(os.path.normcase(str(Path(value).resolve())))
    # Chromium uses the last value when an option is repeated. Respect that
    # rule rather than treating an earlier stale argument as the active profile.
    return bool(values) and values[-1] == expected


def _cleanup_stale_profile(profile: Path = PROFILE) -> None:
    """Remove stale locks only after confirming this profile has no live browser.

    A browser using the same account can belong to another active task. Never
    terminate it here; owned child processes are reaped by the task dispatcher.
    """
    import subprocess

    if sys.platform != "win32":
        return  # No process inventory is available here to prove locks stale.
    profile = profile.resolve()
    ps = ("$ErrorActionPreference = 'Stop'; "
          "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe' OR "
          "Name='chromium.exe' OR Name='msedge.exe' OR Name='headless_shell.exe'\" | "
          "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress")
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
             "-Command", ps], stdin=subprocess.DEVNULL, capture_output=True,
            text=True, timeout=30, **hidden_subprocess_options())
        if result.returncode != 0 or (result.stderr or "").strip():
            raise ValueError("浏览器进程查询失败")
        raw = json.loads(result.stdout or "[]")
        processes = raw if isinstance(raw, list) else [raw]
        for item in processes:
            if (not isinstance(item, dict) or type(item.get("ProcessId")) is not int
                    or item["ProcessId"] <= 0 or not isinstance(item.get("CommandLine"), str)
                    or not item["CommandLine"].strip()):
                raise ValueError("浏览器进程信息不可读")
            if _commandline_uses_profile(item["CommandLine"], profile):
                raise RuntimeError("当前账号的签到浏览器仍在使用，请先关闭对应浏览器或停止相关监控，再重新登录。")
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("无法确认签到浏览器是否仍在使用，已保留登录目录；请关闭相关浏览器后重试。") from exc
    for lock in ("lockfile", "SingletonLock", "SingletonCookie", "SingletonSocket"):
        path = profile / lock
        try:
            if path.exists() or path.is_symlink():
                path.unlink()
        except OSError:
            pass


def login(stop=None) -> int:
    if stop is not None and stop.is_set():
        return 0
    configure_browser_runtime()
    secrets = load_secrets()
    profile = account_profile(secrets)
    _cleanup_stale_profile(profile)
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(str(profile), headless=False, args=_browser_args(),
                                                    **external_subprocess_kwargs())
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        def wait(milliseconds):
            # Keep Playwright dispatching events while allowing window close to
            # cancel manual login instead of waiting for its three-minute limit.
            remaining = milliseconds
            while remaining > 0:
                if stop is not None and stop.is_set():
                    return False
                step = min(100, remaining)
                page.wait_for_timeout(step)
                remaining -= step
            return stop is None or not stop.is_set()

        page.goto(START, wait_until="domcontentloaded", timeout=60000)
        if not wait(3000):
            ctx.close()
            return 0

        deadline = time.time() + 180
        attempted = set()
        while time.time() < deadline:
            if stop is not None and stop.is_set():
                ctx.close()
                return 0
            url = page.url
            host = _https_host(url)
            if host in _ATTENDANCE_SSO_HOSTS:
                if host not in attempted:
                    if not wait(1500):
                        ctx.close()
                        return 0
                    attempted.add(host)
                    try_sso_login(page, secrets)
            elif host == _ATTENDANCE_HOST:
                token = _attendance_session_id(page)
                if token and _attendance_view_visible(page) and _https_host(page.url) == _ATTENDANCE_HOST:
                    print("[OK] 已确认登录态")
                    (ROOT / "session_local.json").write_text(
                        json.dumps({"x_auth_token": token, "captured_at": time.time()},
                                   ensure_ascii=False, indent=2), encoding="utf-8")
                    print("[i] 已保存到 session_local.json")
                    ctx.close()
                    return 0
                print("[i] 正在等上课啦加载课堂签到入口")
            else:
                print(f"[i] 当前页面: {url[:90]}")
            print("    (180秒内自动检测, 也可手动在浏览器里完成任何操作)")
            if not wait(5000):
                ctx.close()
                return 0

        print("[!] 超时未确认登录态; profile 已保留, 可重跑")
        ctx.close()
        return 1


def _location() -> tuple[float, float]:
    cfgp = ROOT / "config.yaml"
    if cfgp.exists():
        try:
            loc = (yaml.safe_load(cfgp.read_text(encoding="utf-8")) or {}).get("location") or {}
            return float(loc.get("lat", DEFAULT_LAT)), float(loc.get("lng", DEFAULT_LNG))
        except Exception:  # noqa: BLE001
            pass
    return DEFAULT_LAT, DEFAULT_LNG


def ensure_logged_in(page, secrets: dict, timeout_s: int = 120) -> str:
    try:
        page.goto(START, wait_until="domcontentloaded", timeout=60000)
    except PlaywrightTimeoutError:
        raise SignPreparationError("打开上课啦超时，请检查网络后重试；本次未提交。") from None
    deadline = time.monotonic() + timeout_s
    attempted = set()
    entry_deadline = None
    while time.monotonic() < deadline:
        host = _https_host(page.url)
        if host in _ATTENDANCE_SSO_HOSTS:
            entry_deadline = None
            if host not in attempted:
                # Leave rejected credentials or a verification prompt to the user.
                attempted.add(host)
                try_sso_login(page, secrets)
        elif host == _ATTENDANCE_HOST:
            try:
                token = _attendance_session_id(page)
                if token and _attendance_view_visible(page) and _https_host(page.url) == _ATTENDANCE_HOST:
                    return token
                # A saved sessionId may precede an asynchronous SSO redirect.
                if token and entry_deadline is None:
                    entry_deadline = min(deadline, time.monotonic() + 15)
            except PlaywrightError as exc:
                if "Execution context was destroyed" not in str(exc):
                    raise
            if entry_deadline is not None and time.monotonic() >= entry_deadline:
                break
        page.wait_for_timeout(100)
    if entry_deadline is not None and _https_host(page.url) == _ATTENDANCE_HOST:
        raise SignPreparationError("上课啦未加载「课堂签到」入口，请检查页面或重新登录签到；本次未提交。")
    raise SignPreparationError("未确认上课啦登录成功，请核对学号和密码并完成网页验证；本次未提交。")


def click_visible(page, selector: str, text: str | None = None, timeout_ms: int = 8000,
                  *, human_path: bool = False):
    """Click first visible element matching selector (and exact text if given).

    With human_path (and behavior boosting enabled), move the pointer along a
    short randomized approach before the click instead of teleporting, so the
    captcha's behavior collector sees plausible pre-click movement.
    """
    locator = page.locator(selector)
    if text is not None:
        locator = locator.filter(has_text=re.compile(r"^\s*" + re.escape(text) + r"\s*$"))
    target = locator.filter(visible=True).first
    if human_path:
        _approach_pointer(page, target)
    with captcha_boost.automatic_input(page):
        target.click(timeout=timeout_ms)
    return target


def _pointer_move_to(page, x: float, y: float, *, slow: bool = False) -> None:
    """Glide the virtual pointer to (x, y) continuously from where it is.

    Every hop is interpolated from the last known position (tracked in
    captcha_boost), so the collected mousemove stream has no teleport gaps
    between wander bursts, keypad digits and the submit click.
    """
    try:
        width = page.evaluate("innerWidth") or 1280
        height = page.evaluate("innerHeight") or 720
    except PlaywrightError:
        width, height = 1280, 720
    x = min(max(x, 3), width - 3)
    y = min(max(y, 3), height - 3)
    start = captcha_boost.pointer_position(page) or (width * 0.5, height * 0.45)
    distance = math.hypot(x - start[0], y - start[1])
    steps = max(2, min(8, int(distance / 45) + 2))
    # slight lateral bow so the glide is not ruler-straight
    bow = random.uniform(-0.12, 0.12) * distance
    ang = math.atan2(y - start[1], x - start[0])
    px, py = start[0] - math.sin(ang) * bow, start[1] + math.cos(ang) * bow
    for i in range(1, steps + 1):
        if captcha_boost.manual_for(page):
            return
        u = i / steps
        # ease-out: smaller steps near the target, like a real correction
        s = 1 - (1 - u) ** 2 if not slow else u * u * (3 - 2 * u)
        ix = (1 - s) ** 2 * start[0] + 2 * (1 - s) * s * px + s ** 2 * x
        iy = (1 - s) ** 2 * start[1] + 2 * (1 - s) * s * py + s ** 2 * y
        with captcha_boost.automatic_input(page):
            page.mouse.move(ix, iy)
        captcha_boost.note_pointer(page, ix, iy)
        page.wait_for_timeout(random.randint(20, 55) if not slow else random.randint(60, 160))
    captcha_boost.note_pointer(page, x, y)


def _approach_pointer(page, target) -> None:
    """Continuous pointer glide toward a locator before clicking it."""
    if not captcha_boost.enabled_for(page) or captcha_boost.manual_for(page):
        return
    try:
        box = target.bounding_box(timeout=1500)
        if not box:
            return
        _pointer_move_to(page, box["x"] + box["width"] / 2,
                         box["y"] + box["height"] / 2)
    except PlaywrightError:
        pass


def _idle_wander(page) -> None:
    """A few slow pointer moves while the prewarmed page waits for a code.

    Keeps the captcha's behavior stream non-empty during long idle waits so
    the silent (traceless) risk scoring sees an attended session. Moves are
    continuous from the pointer's last position; never fatal and no clicks
    are produced.
    """
    try:
        for _ in range(random.randint(2, 5)):
            _pointer_move_to(page, random.uniform(120, 900), random.uniform(200, 640),
                             slow=True)
    except PlaywrightError:
        pass


KEYPAD = "button, .van-button, [role=button]"


def _is_sign_endpoint(url: str, method: str, code: str) -> bool:
    parsed = urlparse(url)
    return (parsed.scheme == "https" and parsed.hostname == "skl.hdu.edu.cn"
            and parsed.path.rstrip("/") == "/api/ali-nvc/captcha-verify"
            and method == "POST"
            and parse_qs(parsed.query).get("code") == [code])


def is_sign_response(response, code: str) -> bool:
    # Keep the response URL check as well (redirects cannot establish success).
    return _is_sign_endpoint(response.url, response.request.method, code)


CAPTCHA_CONTAINERS = (
    ".nc-container, .nc_scale, #baxia-dialog-content, "
    "#aliyunCaptcha-window-popup, #aliyunCaptcha-sliding-wrapper"
)
VERIFICATION_ACTION = re.compile(
    r"^\s*(?:点击(?:按钮)?(?:开始|进行|完成)?(?:智能)?验证|"
    r"点此(?:开始|进行)?验证|我不是机器人|验证(?:您|你)是真人)\s*$"
)
VERIFICATION_MESSAGE = re.compile(
    r"(?:请完成|需要|要求|失败|错误|未通过|不通过|重试).{0,12}"
    r"(?:验证|验证码|人机|安全|滑块|captcha)"
    r"|(?:验证|验证码|人机验证|安全验证|滑块|captcha).{0,12}"
    r"(?:失败|错误|未通过|不通过|重试|请完成|需要|要求|required|challenge)",
    re.I,
)


def _click_verification(page, check_cancelled) -> bool:
    """Try one ordinary click on an explicitly labelled Alibaba verification control.

    True means attempted, not passed. The caller must never retry a click whose
    result is uncertain. Sliders, image challenges and unlabelled controls are
    left for the user.
    """
    if captcha_boost.manual_for(page):
        return False
    for frame in page.frames:
        try:
            owner = frame
            while owner.parent_frame is not None:
                if not owner.frame_element().is_visible():
                    break
                owner = owner.parent_frame
            else:
                scopes = frame.locator(CAPTCHA_CONTAINERS).filter(visible=True)
                candidates = scopes.get_by_role("button", name=VERIFICATION_ACTION).or_(
                    scopes.get_by_role("checkbox", name=VERIFICATION_ACTION)).or_(
                    scopes.get_by_text(VERIFICATION_ACTION))
                target = candidates.filter(visible=True).first
                if target.count() and target.is_enabled():
                    checked = target.evaluate("""el => {
                        const role = el.closest('[role=checkbox]');
                        if (role?.getAttribute('aria-checked') === 'true') return true;
                        const label = el.closest('label');
                        const input = el.matches('input[type=checkbox]') ? el : label?.control;
                        return input?.type === 'checkbox' && input.checked;
                    }""")
                    if checked:
                        continue
                    check_cancelled()
                    try:
                        box = target.bounding_box(timeout=500)
                        if box:
                            # Arm the second-challenge boost at the real click
                            # position before the click itself.
                            captcha_boost.note_click_for(
                                page, box["x"] + box["width"] / 2,
                                box["y"] + box["height"] / 2)
                    except PlaywrightError:
                        pass
                    try:
                        with captcha_boost.automatic_input(page):
                            target.click(timeout=1000)
                    except PlaywrightError:
                        # The click may have been delivered before a navigation.
                        captcha_boost.manual_takeover_for(page)
                    return True
        except PlaywrightError:
            # A verification frame may disappear or navigate during inspection.
            continue
    return False


def _captcha_visible(page) -> bool:
    """Detect visible verification UI without treating it as a sign-in result."""
    selector = (
        CAPTCHA_CONTAINERS + ", "
        "iframe[src*='captcha' i], iframe[src*='baxia' i], "
        "iframe[title*='验证码'], iframe[title*='安全验证'], "
        "iframe[title*='captcha' i], iframe[aria-label*='captcha' i], "
        "[role=dialog]:has-text('安全验证'), [role=dialog]:has-text('验证码')"
    )
    try:
        return bool(page.locator(selector).evaluate_all("""els => els.some(el => {
            const rect = el.getBoundingClientRect();
            if (rect.width <= 1 || rect.height <= 1) return false;
            let left = rect.left, right = rect.right, top = rect.top, bottom = rect.bottom;
            for (let node = el; node; node = node.parentElement) {
                const style = getComputedStyle(node);
                if (style.display === 'none' || style.visibility === 'hidden' ||
                    style.visibility === 'collapse' || Number(style.opacity) === 0 ||
                    node.getAttribute('aria-hidden') === 'true') return false;
                if (node !== el) {
                    const bounds = node.getBoundingClientRect();
                    if (['hidden', 'clip', 'scroll', 'auto'].includes(style.overflowX)) {
                        left = Math.max(left, bounds.left); right = Math.min(right, bounds.right);
                    }
                    if (['hidden', 'clip', 'scroll', 'auto'].includes(style.overflowY)) {
                        top = Math.max(top, bounds.top); bottom = Math.min(bottom, bounds.bottom);
                    }
                }
            }
            return right - left > 1 && bottom - top > 1;
        })"""))
    except PlaywrightError:
        # The page may navigate while a verification result is arriving.
        return False


def _wait_for_sign_result(page, code, stage, check_cancelled, *, timeout_s=120) -> SignResult:
    responses = deque()
    requests = set()

    def on_request(request):
        if _is_sign_endpoint(request.url, request.method, code):
            requests.add(request)

    def on_response(response):
        if response.request in requests and is_sign_response(response, code):
            responses.append(response)

    verification = False
    successful = False

    def require_verification():
        nonlocal verification
        captcha_boost.manual_takeover_for(page)
        if verification:
            return
        verification = True
        stage("verification_required", time.time())
        print("[i] 签到等待验证：请查看浏览器并完成验证码，完成后自动核对签到结果。", flush=True)
        try:
            page.bring_to_front()
        except PlaywrightError:
            pass

    # Capture requests as well as responses so a late response to an earlier
    # submission of the same code cannot satisfy this one.
    page.on("request", on_request)
    page.on("response", on_response)
    try:
        check_cancelled()
        click_visible(page, KEYPAD, "签到", timeout_ms=6000, human_path=True)
        stage("submit", time.time())
        started = time.monotonic()
        deadline = started + timeout_s
        pending_result = None
        settle_at = started
        verification_clicked_at = None
        while time.monotonic() < deadline:
            check_cancelled()
            if verification_clicked_at is None and _click_verification(page, check_cancelled):
                verification_clicked_at = time.monotonic()
                stage("verification_clicked", time.time())
                print("[i] 已尝试点击验证按钮一次，等待平台确认。", flush=True)
            click_pending = (verification_clicked_at is not None
                            and time.monotonic() - verification_clicked_at < 3)
            visible = _captcha_visible(page)
            if visible and not click_pending:
                require_verification()
            while responses:
                response = responses.popleft()
                try:
                    payload = response.json()
                except (ValueError, PlaywrightError):
                    payload = None
                result = classify_response(code, response.status, payload)
                if VERIFICATION_MESSAGE.search(result.message):
                    # A challenge/rejected attempt is intermediate. Only the
                    # site's later attendance response can confirm completion.
                    if not click_pending:
                        require_verification()
                    pending_result = None
                elif result.status == "success":
                    successful = True
                    stage("response", time.time())
                    return result
                else:
                    pending_result = result
                    # Give UI triggered by a response time to show its challenge.
                    settle_at = time.monotonic() + 1
            if visible:
                # A business rejection may arrive just before the widget closes.
                # Preserve it and let the UI settle instead of losing the result.
                settle_at = time.monotonic() + 1
            if pending_result is not None and not visible and time.monotonic() >= settle_at:
                stage("response", time.time())
                return pending_result
            page.wait_for_timeout(100)
        return SignResult("unknown", code, "等待验证或签到结果超时，请到平台核对；未自动重复提交")
    except PlaywrightTimeoutError:
        return SignResult("unknown", code, "未能确认本次签到提交，请到平台核对")
    finally:
        captcha_boost.finish_round_for(page, success=successful)
        for event, handler in (("request", on_request), ("response", on_response)):
            try:
                page.remove_listener(event, handler)
            except PlaywrightError:
                pass


def _attendance_tab(page):
    return page.get_by_text("课堂签到", exact=True).filter(visible=True).first


def _on_sign_in_page(url: str) -> bool:
    if _https_host(url) != _ATTENDANCE_HOST:
        return False
    parsed = urlparse(url)
    return any(path.rstrip("/") == "/sign/in"
               for path in (parsed.path, parsed.fragment.split("?", 1)[0]))


def _attendance_view_visible(page) -> bool:
    return _on_sign_in_page(page.url) or _attendance_tab(page).is_visible()


def open_sign_in(page) -> None:
    stage = "入口"
    try:
        if _https_host(page.url) != _ATTENDANCE_HOST:
            raise SignPreparationError("上课啦登录已失效，请重新登录签到；本次未提交。")
        if not _on_sign_in_page(page.url):
            print("[i] 打开 课堂签到 ...")
            with captcha_boost.automatic_input(page):
                _attendance_tab(page).click(timeout=15000)
            page.wait_for_url(_on_sign_in_page, timeout=15000)
        stage = "键盘"
        page.locator(KEYPAD).filter(has_text=re.compile(r"^\s*0\s*$")).filter(
            visible=True).first.wait_for(state="visible", timeout=15000)
        if not _on_sign_in_page(page.url):
            raise SignPreparationError("签到页面已跳转，请重新登录签到；本次未提交。")
    except PlaywrightTimeoutError:
        if _https_host(page.url) in _ATTENDANCE_SSO_HOSTS:
            raise SignPreparationError("上课啦页面跳回统一身份认证，请重新登录签到；本次未提交。") from None
        raise SignPreparationError(f"上课啦未加载「课堂签到」{stage}，请检查页面；本次未提交。") from None


def prepared_page_ready(page) -> bool:
    """Check local session and keypad readiness without refreshing or submitting.

    A local token cannot prove that the server still accepts the session. The
    matched business response remains the authority for the sign-in result.
    """
    try:
        if not _on_sign_in_page(page.url):
            return False
        return page.evaluate("""selector => {
            if (!localStorage.getItem('sessionId')) return false;
            const buttons = Array.from(document.querySelectorAll(selector));
            const usable = (el, label) => {
                if (el.textContent.trim() !== label) return false;
                // Submit may stay disabled until all four digits are entered.
                if (label === '0' && (el.disabled ||
                    el.getAttribute('aria-disabled') === 'true')) return false;
                const rect = el.getBoundingClientRect();
                if (rect.width <= 0 || rect.height <= 0) return false;
                for (let node = el; node; node = node.parentElement) {
                    const style = getComputedStyle(node);
                    if (style.display === 'none' || style.visibility === 'hidden' ||
                        Number(style.opacity) === 0) return false;
                }
                return true;
            };
            return buttons.some(el => usable(el, '0')) &&
                   buttons.some(el => usable(el, '签到'));
        }""", KEYPAD) is True
    except (PlaywrightError, ValueError):
        return False


def sign_with_code(ctx, page, code: str, *, on_stage=None, cancelled=None) -> SignResult:
    stage = on_stage or (lambda name, when: None)
    def check_cancelled():
        if cancelled is not None and cancelled():
            raise InterruptedError("监控已停止，未继续提交")

    check_cancelled()
    captcha_boost.begin_round_for(page)
    open_sign_in(page)
    stage("page_ready", time.time())
    print("[i] 已进入签到键盘页")

    hesitated = False
    for d in code:
        check_cancelled()
        click_visible(page, KEYPAD, d, timeout_ms=6000, human_path=True)
        if (captcha_boost.enabled_for(page) and not captcha_boost.manual_for(page)
                and not hesitated and random.random() < 0.25):
            # one short hesitation while entering the code, like a re-check;
            # real-time sleep so the input-event gap shows up in behavior
            # data without advancing the page event loop
            time.sleep(random.uniform(0.15, 0.35))
            hesitated = True
    stage("digits_entered", time.time())
    print(f"[i] 已输入四位签到码 {diagnostic_sign_code(code)}")

    return _wait_for_sign_result(page, code, stage, check_cancelled)


def sign(argv=None) -> int:
    parser = argparse.ArgumentParser(description="在本人已授权的课堂中提交四位签到码")
    parser.add_argument("code")
    parser.add_argument("--result-file", type=Path)
    parser.add_argument("--progress-file", type=Path)
    parser.add_argument("--captcha-boost", choices=("on", "off"), default="on",
                        help="二次挑战注入扰动真人轨迹(默认开启)")
    args = parser.parse_args(argv)
    code = args.code
    if len(code) != 4 or not code.isascii() or not code.isdigit():
        parser.error("签到码必须是四位数字")
    try:
        configure_browser_runtime()
        secrets = load_secrets()
        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(str(account_profile(secrets)),
                                                        headless=False, args=_browser_args(),
                                                        **external_subprocess_kwargs())
            try:
                lat, lng = _location()
                ctx.grant_permissions(["geolocation"], origin="https://skl.hdu.edu.cn")
                ctx.set_geolocation({"latitude": lat, "longitude": lng, "accuracy": 30})
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                captcha_boost.install_for(page, enabled=args.captcha_boost == "on")
                ensure_logged_in(page, secrets)
                print("[i] 登录态已确认")
                def on_stage(name, when):
                    if name == "verification_required" and args.progress_file:
                        write_json(args.progress_file, {"status": name, "sign_code": code})

                result = sign_with_code(ctx, page, code, on_stage=on_stage)
            finally:
                ctx.close()
    except SignPreparationError as exc:
        result = SignResult("failure", code, str(exc))
    except Exception as exc:  # A browser error cannot establish a sign-in outcome.
        result = SignResult("unknown", code, f"浏览器操作异常：{exc}")
    print(f"[结果] {result.status}: {result.message}")
    if args.result_file:
        result.write(args.result_file)
    return result.exit_code


def serve_prepared(ctx, page, secrets: dict, directory: Path) -> None:
    """Read-only preparation, followed only by explicitly confirmed requests."""
    ensure_logged_in(page, secrets)
    open_sign_in(page)
    ready_at = time.time()
    write_json(directory / "ready.json", {"status": "ready", "page_ready_at": ready_at})
    last_request = 0
    next_wander = time.monotonic() + random.uniform(25, 45)
    while not (directory / "stop.json").exists():
        request_path = directory / f"request-{last_request + 1}.json"
        if not request_path.exists():
            # Keep the silent risk scoring fed during long idle waits: a few
            # slow pointer moves every so often make the session look attended.
            if (captcha_boost.enabled_for(page) and not captcha_boost.manual_for(page)
                    and time.monotonic() >= next_wander):
                _idle_wander(page)
                next_wander = time.monotonic() + random.uniform(25, 45)
            # Dispatch observation and route callbacks while waiting for a code.
            page.wait_for_timeout(50)
            continue
        request = json.loads(request_path.read_text(encoding="utf-8"))
        sequence = request.get("id")
        if type(sequence) is not int or sequence != last_request + 1:
            raise ValueError("无效的签到请求序号")
        code = request.get("code")
        if not isinstance(code, str) or len(code) != 4 or not code.isascii() or not code.isdigit():
            raise ValueError("签到码必须是四位数字")
        last_request = sequence
        if (directory / "stop.json").exists():
            return
        timings = {"detected_at": request.get("detected_at"), "prewarmed_page_ready": ready_at}
        def on_stage(name, when):
            timings[name] = when
            if name == "verification_required":
                write_json(directory / f"progress-{sequence}.json", {"status": name, "sign_code": code})

        session_invalid = False
        try:
            if not prepared_page_ready(page):
                session_invalid = True
                result = SignResult("failure", code,
                                    "签到页或登录态已失效，请重新登录；本次未输入或提交签到码")
            else:
                result = sign_with_code(ctx, page, code,
                                        on_stage=on_stage,
                                        cancelled=lambda: (directory / "stop.json").exists())
        except SignPreparationError as exc:
            session_invalid = True
            result = SignResult("failure", code, str(exc))
        except Exception as exc:
            session_invalid = True
            result = SignResult("unknown", code, f"浏览器操作异常：{exc}")
        write_json(directory / f"timing-{sequence}.json", timings)
        result.write(directory / f"result-{sequence}.json")
        if session_invalid or (directory / "stop.json").exists():
            return
        # Return to a fresh, empty keypad while waiting for another sign-in in
        # the same lesson. Never reuse the previous four digits or response.
        ensure_logged_in(page, secrets)
        open_sign_in(page)
        ready_at = time.time()


def sign_worker(argv=None) -> int:
    parser = argparse.ArgumentParser(description="预备签到页并等待本次监控确认的签到码")
    parser.add_argument("--exchange-dir", type=Path, required=True)
    parser.add_argument("--captcha-boost", choices=("on", "off"), default="on",
                        help="二次挑战注入扰动真人轨迹(默认开启)")
    args = parser.parse_args(argv)
    directory = args.exchange_dir.resolve(strict=True)
    try:
        configure_browser_runtime()
        secrets = load_secrets()
        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(str(account_profile(secrets)), headless=False, args=_browser_args(),
                                                        **external_subprocess_kwargs())
            try:
                lat, lng = _location()
                ctx.grant_permissions(["geolocation"], origin="https://skl.hdu.edu.cn")
                ctx.set_geolocation({"latitude": lat, "longitude": lng, "accuracy": 30})
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                captcha_boost.install_for(page, enabled=args.captcha_boost == "on")
                serve_prepared(ctx, page, secrets, directory)
            finally:
                ctx.close()
    except Exception as exc:
        write_json(directory / "ready.json", {"status": "error", "message": str(exc)[:240]})
        return 2
    return 0
