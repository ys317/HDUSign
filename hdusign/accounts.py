"""Local account vault using Windows DPAPI, scoped to the current Windows user."""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import hashlib
import json
import math
import os
from pathlib import Path
import shutil

SESSION_ENV = "HDUSIGN_SESSION_CREDENTIALS"


def protect(text, decrypt=False):
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    raw = base64.b64decode(text, validate=True) if decrypt else text.encode("utf-8")
    buffer = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise OSError("无法读取或加密密码，请在原 Windows 用户下运行或重新输入密码。")
    try:
        result = ctypes.string_at(target.data, target.size)
        return result.decode("utf-8") if decrypt else base64.b64encode(result).decode("ascii")
    finally:
        kernel.LocalFree(target.data)


def read_json(path):
    return json.loads(path.read_text("utf-8-sig")) if path.exists() else {}


def write_json(path, data):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    temporary.replace(path)


def session_credentials(root, credentials, *, environment=None):
    # Account workers inherit encrypted credentials without writing
    # an unremembered password to disk or passing it on a command line.
    payload = dict(root=str(Path(root).resolve()), credentials=credentials)
    target = os.environ if environment is None else environment
    target[SESSION_ENV] = protect(json.dumps(payload, ensure_ascii=False))


def load_credentials(root, use_session=True):
    root = Path(root)
    data = read_json(root / "secrets_local.json")
    if "skl_password_dpapi" in data:
        data["skl_password"] = protect(data.pop("skl_password_dpapi"), True)
    vault = AccountStore(root)
    if vault.last and vault.last in vault.accounts:
        data.update(skl_username=vault.last, skl_password=vault.password(vault.last))
    if use_session and os.environ.get(SESSION_ENV):
        payload = json.loads(protect(os.environ[SESSION_ENV], True))
        if payload.get("root") == str(root.resolve()):
            data.update(payload["credentials"])
    return data


def remove_profile(root, relative):
    root = Path(root).resolve()
    target = root / relative
    # Validate the exact recursive removal target and reject directory links.
    if not target.resolve().is_relative_to(root) or target.resolve() == root:
        raise OSError("浏览器目录不在软件目录内，未删除。")
    if not target.exists():
        return
    for parent in (target, *target.parents):
        if parent == root:
            break
        if parent.is_symlink() or parent.is_junction():
            raise OSError("浏览器目录包含链接，未删除。")
    for directory, directories, _ in os.walk(target, followlinks=False):
        for name in directories:
            child = Path(directory) / name
            if child.is_symlink() or child.is_junction():
                raise OSError("浏览器目录包含链接，未删除。")
    shutil.rmtree(target)


class AccountStore:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / "accounts.json"
        value = read_json(self.path)
        self.accounts = value.get("accounts", {})
        self.last = value.get("last", "")
        if (not isinstance(self.accounts, dict) or not isinstance(self.last, str)
                or any(not isinstance(k, str) or not isinstance(v, str)
                       for k, v in self.accounts.items())):
            raise ValueError("已保存账号文件格式错误。")
        usage = value.get("last_used", {})
        self.last_used = {}
        if isinstance(usage, dict):
            self.last_used = {username: at for username, at in usage.items()
                              if username in self.accounts and not isinstance(at, bool)
                              and isinstance(at, (int, float)) and math.isfinite(at) and at > 0}

    def remember_use(self, username, at):
        if username not in self.accounts:
            return
        usage = dict(self.last_used)
        usage[username] = at
        write_json(self.path, {"accounts": self.accounts, "last": self.last, "last_used": usage})
        self.last_used = usage

    def clear_last(self):
        write_json(self.path, {"accounts": self.accounts, "last": "", "last_used": self.last_used})
        self.last = ""

    def password(self, username):
        return protect(self.accounts[username], True)

    def save(self, username, password):
        records = dict(self.accounts)
        records[username] = protect(password)
        write_json(self.path, {"accounts": records, "last": username, "last_used": self.last_used})
        self.accounts, self.last = records, username

    def delete(self, username):
        identity = hashlib.sha256(username.encode("utf-8")).hexdigest()
        remove_profile(self.root, Path("browser_profile") / "accounts" / identity)
        session = self.root / "live_session.json"
        saved = read_json(session)
        if saved and (saved.get("username") == username or not saved.get("username")):
            remove_profile(self.root, Path("live_profile"))
            session.unlink(missing_ok=True)
        secrets_path = self.root / "secrets_local.json"
        secrets = read_json(secrets_path)
        if secrets.get("skl_username") == username:
            for key in ("skl_username", "skl_password", "skl_password_dpapi"):
                secrets.pop(key, None)
            write_json(secrets_path, secrets)
        records = dict(self.accounts)
        records.pop(username, None)
        last = "" if self.last == username else self.last
        usage = {key: at for key, at in self.last_used.items() if key != username}
        write_json(self.path, {"accounts": records, "last": last, "last_used": usage})
        self.accounts, self.last, self.last_used = records, last, usage
