"""Shared browser login helpers for HDU's 上课啦."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys

import yaml
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from hdusign.processes import hidden_subprocess_options
from hdusign.runtime import data_root as application_root

ROOT = application_root()
PROFILE = ROOT / "browser_profile"


def account_profile(secrets: dict) -> Path:
    """Locate legacy account profiles for cleanup when an account is deleted."""
    username = str(secrets.get("skl_username", "")).strip()
    if not username:
        return PROFILE
    identity = hashlib.sha256(username.encode("utf-8")).hexdigest()
    return PROFILE / "accounts" / identity


def load_secrets() -> dict:
    from hdusign.accounts import load_credentials
    return load_credentials(ROOT)


def try_sso_login(page, secrets: dict) -> bool:
    """Fill the HDU SSO form. Returns True if a login submit was attempted.

    The current 统一身份认证 page is an Angular form whose 登录 button stays
    disabled until both fields register input, so fields are clicked before
    filling and the button is awaited. Older CAS layouts fall back to heuristics.
    """
    user = secrets["skl_username"]
    pwd = secrets["skl_password"]

    user_selectors = ["input[name=username]", "#username", "input[name=account]",
                      "input[type=text]:not([name*=ver])", "input[placeholder*=账号]",
                      "input[placeholder*=学号]", "input[placeholder*=用户名]"]
    filled_user = None
    for sel in user_selectors:
        el = page.query_selector(sel)
        if el and el.is_visible():
            el.click()
            el.fill(user)
            filled_user = sel
            break
    pw_selectors = ["input[type=password]", "#password", "input[name=password]"]
    filled_pw = None
    for sel in pw_selectors:
        el = page.query_selector(sel)
        if el and el.is_visible():
            el.click()
            el.fill(pwd)
            filled_pw = sel
            break
    if not (filled_user and filled_pw):
        print(f"[!] 没找齐输入框 (user={filled_user}, pwd={filled_pw}), 请手动在浏览器里完成登录")
        return False

    print(f"[i] 自动填充登录表单 ({filled_user} / {filled_pw})")
    try:
        page.wait_for_function(
            "() => { const b = document.querySelector('button.login-button');"
            " return !b || (!b.disabled && !b.classList.contains('disabled')); }", timeout=8000)
    except PlaywrightTimeoutError:
        print("[!] 登录按钮未启用，请在浏览器中手动完成登录")
        return False
    for sel in ["button.login-button", "button:has-text('登 录')", "button:has-text('登录')",
                "#login_submit", "input[type=submit]", "button[type=submit]", ".login-btn", "#loginBtn"]:
        el = page.query_selector(sel)
        if el and el.is_visible():
            try:
                el.click(timeout=5000)
            except PlaywrightTimeoutError:
                print(f"[!] 无法点击登录按钮 {sel}，请在浏览器中手动完成登录")
                return False
            print(f"[i] 点击登录: {sel}")
            return True
    print("[i] 未找到登录按钮, 尝试回车提交")
    page.keyboard.press("Enter")
    return True


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
                raise RuntimeError("当前账号的旧版账号浏览器仍在使用，请先关闭对应浏览器或停止相关监控，再重新登录。")
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("无法确认旧版账号浏览器是否仍在使用，已保留登录目录；请关闭相关浏览器后重试。") from exc
    for lock in ("lockfile", "SingletonLock", "SingletonCookie", "SingletonSocket"):
        path = profile / lock
        try:
            if path.exists() or path.is_symlink():
                path.unlink()
        except OSError:
            pass
