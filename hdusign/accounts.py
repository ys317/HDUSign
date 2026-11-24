"""Local account vault using Windows DPAPI or user-scoped POSIX vault keys."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import secrets
import shutil
import struct
import sys

SESSION_ENV = "HDUSIGN_SESSION_CREDENTIALS"
_POSIX_PREFIX = "v1:"
_POSIX_ERROR = "无法读取或加密密码，请在原 Linux 用户下运行或重新输入密码。"


def _protect_win32(text, decrypt=False):
    import ctypes
    from ctypes import wintypes

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


def _posix_vault_key_path() -> Path:
    override = os.environ.get("HDUSIGN_VAULT_KEY_PATH", "").strip()
    if override:
        return Path(override)
    base = os.environ.get("XDG_CONFIG_HOME", "").strip()
    config_root = Path(base) if base else (Path.home() / ".config")
    return config_root / "HDUSign" / "vault.key"


def _read_posix_key(path: Path) -> bytes:
    if path.is_symlink():
        raise OSError(_POSIX_ERROR)
    key = path.read_bytes()
    if len(key) != 32:
        raise OSError(_POSIX_ERROR)
    return key


def _posix_vault_key(create: bool) -> bytes:
    path = _posix_vault_key_path()
    if path.is_symlink():
        raise OSError(_POSIX_ERROR)
    if path.is_file():
        return _read_posix_key(path)
    if not create:
        raise OSError(_POSIX_ERROR)
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            os.chmod(parent, 0o700)
        except OSError:
            pass
    key = secrets.token_bytes(32)
    tmp = parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        try:
            os.write(fd, key)
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.link(tmp, path)
            return key
        except FileExistsError:
            return _read_posix_key(path)
        except OSError:
            try:
                direct_fd = os.open(path, flags, 0o600)
            except FileExistsError:
                return _read_posix_key(path)
            try:
                os.write(direct_fd, key)
                os.fsync(direct_fd)
            finally:
                os.close(direct_fd)
            return key
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _xor_keystream(enc_key: bytes, nonce: bytes, data: bytes) -> bytes:
    if not data:
        return b""
    out = bytearray(len(data))
    offset = 0
    counter = 0
    while offset < len(data):
        block = hmac.new(enc_key, nonce + struct.pack(">I", counter), hashlib.sha256).digest()
        take = min(len(block), len(data) - offset)
        for index in range(take):
            out[offset + index] = data[offset + index] ^ block[index]
        offset += take
        counter += 1
    return bytes(out)


def _protect_posix(text, decrypt=False):
    master = _posix_vault_key(create=not decrypt)
    enc_key = hmac.new(master, b"hdusign-vault-v1-enc", hashlib.sha256).digest()
    mac_key = hmac.new(master, b"hdusign-vault-v1-mac", hashlib.sha256).digest()
    if decrypt:
        if not isinstance(text, str) or not text.startswith(_POSIX_PREFIX):
            raise OSError(_POSIX_ERROR)
        try:
            blob = base64.b64decode(text[len(_POSIX_PREFIX):], validate=True)
        except ValueError as exc:
            raise OSError(_POSIX_ERROR) from exc
        if len(blob) < 48:
            raise OSError(_POSIX_ERROR)
        nonce, ciphertext, tag = blob[:16], blob[16:-32], blob[-32:]
        expected = hmac.new(mac_key, b"hdusign-vault-v1" + nonce + ciphertext, hashlib.sha256).digest()
        if not hmac.compare_digest(tag, expected):
            raise OSError(_POSIX_ERROR)
        plaintext = _xor_keystream(enc_key, nonce, ciphertext)
        try:
            return plaintext.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise OSError(_POSIX_ERROR) from exc
    plaintext = text.encode("utf-8")
    nonce = secrets.token_bytes(16)
    ciphertext = _xor_keystream(enc_key, nonce, plaintext)
    tag = hmac.new(mac_key, b"hdusign-vault-v1" + nonce + ciphertext, hashlib.sha256).digest()
    return _POSIX_PREFIX + base64.b64encode(nonce + ciphertext + tag).decode("ascii")


def protect(text, decrypt=False):
    if sys.platform == "win32":
        return _protect_win32(text, decrypt)
    return _protect_posix(text, decrypt)


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


def _is_junction(path: Path) -> bool:
    checker = getattr(path, "is_junction", None)
    return bool(checker()) if checker is not None else False


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
        if parent.is_symlink() or _is_junction(parent):
            raise OSError("浏览器目录包含链接，未删除。")
    for directory, directories, _ in os.walk(target, followlinks=False):
        for name in directories:
            child = Path(directory) / name
            if child.is_symlink() or _is_junction(child):
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
