"""Structured attendance results and isolated browser-task dispatch."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from contextlib import ExitStack
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

from hdusign.processes import hidden_subprocess_options
from hdusign.runtime import application_root, data_root

ROOT = data_root()

# Keep the task deadline long enough for a user to complete a challenge while
# preventing malformed configuration from creating an effectively unbounded
# worker or a zero/negative deadline.
DEFAULT_TIMEOUT_SECONDS = 180
MIN_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 1800


def diagnostic_sign_code(code: str) -> str:
    """Reveal codes in operation diagnostics only with explicit local opt-in."""
    return code if os.getenv("ECHOSIGN_DEBUG_SIGN_INPUT") == "1" else "****"


def write_json(path: Path, data: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def browser_command(*args: str) -> list[str]:
    if getattr(sys, "frozen", False):
        prefix = [sys.executable]
    elif os.environ.get("HDUSIGN_ACCOUNT_ROOT"):
        # The child starts in the account data directory. Bootstrap the shared
        # source/dependencies without changing its inherited account or DPAPI
        # session credentials, and without putting a password on the CLI.
        prefix = [sys.executable, "-X", "utf8",
                  str(application_root() / "tools" / "account_worker_bootstrap.py")]
    else:
        prefix = [sys.executable, "-X", "utf8", "-m", "hdusign"]
    return [*prefix, *args]


def verification_progress(path: Path, code: str, notify):
    """One prompt per request, independent of buffered/windowless child logs."""
    sent = False

    def poll():
        nonlocal sent
        if sent or notify is None or not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if (isinstance(data, dict) and data.get("status") == "verification_required"
                and data.get("sign_code") == code):
            sent = True
            notify(code)

    return poll


@dataclass(frozen=True)
class SignResult:
    status: str
    sign_code: str
    message: str

    @property
    def exit_code(self) -> int:
        return {"success": 0, "failure": 1, "unknown": 2}[self.status]

    def write(self, path: Path) -> None:
        write_json(path, asdict(self))

    @classmethod
    def read(cls, path: Path, sign_code: str) -> SignResult:
        data = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(data, dict)
                or data.get("status") not in ("success", "failure", "unknown")
                or data.get("sign_code") != sign_code
                or not isinstance(data.get("message"), str)):
            raise ValueError("签到结果格式无效或签到码不匹配")
        return cls(data["status"], sign_code, data["message"][:240])


def classify_response(sign_code: str, http_status: int, payload: object) -> SignResult:
    """Interpret the business code from the matched HDU sign-in endpoint only.

    HTTP 200, captcha/login responses and arbitrary log text do not prove a sign-in.
    Missing or unrecognized response schemas remain unknown.
    """
    if not 200 <= http_status < 300:
        return SignResult("failure", sign_code, f"签到接口返回 HTTP {http_status}")
    if not isinstance(payload, dict):
        return SignResult("unknown", sign_code, "签到接口未返回可识别的 JSON 结果")
    message = payload.get("msg") or payload.get("message")
    message = " ".join(message.split())[:240] if isinstance(message, str) else ""
    business_code = payload.get("code")
    if type(business_code) not in (int, str):
        return SignResult("unknown", sign_code, "签到响应缺少有效的业务状态码")
    if business_code in (200, "200") and payload.get("success") is not False:
        return SignResult("success", sign_code, message or "签到接口已确认成功")
    return SignResult("failure", sign_code, message or f"签到接口返回业务状态 {business_code}")


class AutoSigner:
    """One worker per monitoring session; only the latest waiting code is kept."""

    def __init__(self, notify, timeout_s: int = 180, stop: threading.Event | None = None,
                 *, prewarm_browser: bool = False, captcha_boost: bool = True):
        self.notify = notify          # callable(reason, text) -> WeChat/console push
        self.timeout_s = timeout_s
        self._condition = threading.Condition()
        self._stop = stop if stop is not None else threading.Event()
        self._pending: tuple[str, float] | None = None
        self._active_code: str | None = None
        self._closing = False
        self._worker: threading.Thread | None = None
        self._prewarm_browser = prewarm_browser
        self._captcha_boost = captcha_boost
        self._prepare_requested = False
        self._prepare_failed = False
        self._session: _BrowserSession | None = None
        self._detected_at: float | None = None

    def _start_worker(self) -> None:
        if self._worker is None:
            self._worker = threading.Thread(target=self._run, name="HDUSign attendance", daemon=False)
            try:
                self._worker.start()
            except Exception:
                self._worker = None
                self._pending = None
                self._prepare_requested = False
                raise

    def prepare(self) -> bool:
        """Warm the sign-in page without providing or submitting any code."""
        with self._condition:
            if (not self._prewarm_browser or self._closing or self._stop.is_set()
                    or self._prepare_failed):
                return False
            if self._session is None:
                self._prepare_requested = True
                self._start_worker()
                self._condition.notify_all()
        return True

    def submit(self, code: str) -> bool:
        # Speech recognition can return arbitrary text.  Only pass an exact
        # four-character ASCII code to the browser worker; ``str.isdigit`` is
        # intentionally avoided because it accepts non-ASCII numerals.
        if (not isinstance(code, str) or len(code) != 4
                or any(char < "0" or char > "9" for char in code)):
            return False
        with self._condition:
            if self._closing or self._stop.is_set():
                return False
            # Repeated recognition while verification is pending belongs to
            # the current request, not a second submission to the platform.
            if code == self._active_code or (self._pending is not None and code == self._pending[0]):
                return True
            self._pending = (code, time.time())
            self._start_worker()
            self._condition.notify_all()
        return True

    def close(self, *, cancel: bool = True) -> None:
        """Reject further work and join the worker; cancellation discards the queue.

        Finite WAV replay may drain normally with cancel=False. A shared stop
        signal or Ctrl+C still cancels the running child during that wait.
        """
        with self._condition:
            self._closing = True
            if cancel:
                self._stop.set()
                self._pending = None
            self._condition.notify_all()
            worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            try:
                worker.join()
            except BaseException:
                self._stop.set()
                with self._condition:
                    self._pending = None
                    self._condition.notify_all()
                worker.join()
                raise

    def _run(self) -> None:
        try:
            while True:
                with self._condition:
                    while (self._pending is None and not self._prepare_requested
                           and not self._closing and not self._stop.is_set()):
                        self._condition.wait(timeout=0.1)
                    if self._stop.is_set() or (self._closing and self._pending is None):
                        return
                    pending, self._pending = self._pending, None
                    self._active_code = pending[0] if pending is not None else None
                    self._prepare_requested = False
                if self._prewarm_browser and self._session is None and not self._prepare_failed:
                    self._prepare_browser()
                if pending is None:
                    continue
                code, self._detected_at = pending
                try:
                    self._sign_one(code)
                except Exception as exc:
                    print(f"[warn] 签到任务异常：{exc}")
                finally:
                    with self._condition:
                        self._active_code = None
        finally:
            try:
                if self._session is not None:
                    self._session.close()
            finally:
                self._session = None
                with self._condition:
                    self._pending = None
                    self._active_code = None
                    self._closing = True

    def _prepare_browser(self) -> None:
        session = _BrowserSession(self._stop, self.timeout_s,
                                  on_verification=self._notify_verification,
                                  captcha_boost=self._captcha_boost)
        self._session = session
        try:
            session.open()
            print("[autosign] 签到页已准备好，等待确认签到码")
        except Exception as exc:
            session.close()
            self._session = None
            self._prepare_failed = True
            if not self._stop.is_set():
                print(f"[autosign] 页面预备未完成，确认码后再打开：{exc}")

    @staticmethod
    def _terminate_child(proc) -> None:
        """Stop this child's process tree, including its Playwright browser."""
        if proc.poll() is not None:
            return
        if sys.platform == "win32":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL, capture_output=True, timeout=5,
                    **hidden_subprocess_options())
            except (OSError, subprocess.TimeoutExpired) as exc:
                print(f"[warn] 浏览器子进程清理失败：{exc}")
        if proc.poll() is None:
            proc.kill()
        proc.wait()

    def _run_child(self, command: list[str], *, progress=None):
        if self._stop.is_set():
            return None
        # Files avoid pipe-reader threads blocking shutdown if a browser inherits
        # a child's stdout/stderr. Result interpretation still uses result.json.
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            with subprocess.Popen(
                    command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, cwd=str(ROOT),
                    **hidden_subprocess_options()) as proc:
                deadline = time.monotonic() + self.timeout_s
                try:
                    while proc.poll() is None:
                        if self._stop.is_set():
                            return None
                        if progress is not None:
                            progress()
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise subprocess.TimeoutExpired(command, self.timeout_s)
                        self._stop.wait(min(0.1, remaining))
                finally:
                    self._terminate_child(proc)
                stdout.seek(0)
                stderr.seek(0)
                return subprocess.CompletedProcess(
                    command, proc.returncode,
                    stdout.read().decode("utf-8", errors="replace"),
                    stderr.read().decode("utf-8", errors="replace"))

    def _sign_one(self, code: str) -> None:
        if self._stop.is_set():
            return
        print(f"[autosign] 收到四位签到码 {diagnostic_sign_code(code)}, 启动自动签到...")
        if self._session is not None and not self._session.is_alive():
            self._session.close()
            self._session = None
        if self._session is not None:
            try:
                result = self._session.submit(code, self._detected_at or time.time())
            except Exception as exc:
                # A request may already have reached the server. Never retry it
                # in a new browser just because the worker/result disappeared.
                result = SignResult("unknown", code, f"无法确认签到结果：{exc}")
                self._session.close()
                self._session = None
                self._prepare_failed = True
            self._notify_result(result)
            return
        cmd = browser_command("--sign", code)
        if not self._captcha_boost:
            cmd.append("--captcha-boost=off")
        result = SignResult("unknown", code, "浏览器未返回明确的签到结果，请到平台核对")
        try:
            # Windowed executables may not expose stdout. Use a private result file
            # for both frozen and source runs; logs never determine success.
            with tempfile.TemporaryDirectory(prefix="hdusign-result-") as temporary:
                result_path = Path(temporary) / "result.json"
                progress_path = Path(temporary) / "progress.json"
                proc = self._run_child(
                    [*cmd, "--result-file", str(result_path), "--progress-file", str(progress_path)],
                    progress=verification_progress(progress_path, code, self._notify_verification))
                if proc is None:
                    print("[autosign] 浏览器任务已停止；已提交的签到请到平台核对")
                    return
                out = (proc.stdout or "") + (proc.stderr or "")
                print("\n".join(line for line in out.splitlines() if line.strip())[-1200:])
                if result_path.exists():
                    result = SignResult.read(result_path, code)
                if result.status == "success" and proc.returncode != 0:
                    result = SignResult("unknown", code, "浏览器异常退出，签到结果需到平台核对")
        except subprocess.TimeoutExpired:
            result = SignResult("unknown", code, "浏览器处理超时，签到结果需到平台核对")
        except (OSError, ValueError) as exc:
            result = SignResult("unknown", code, f"无法确认签到结果：{exc}")

        self._notify_result(result)

    def _notify_verification(self, code: str) -> None:
        if not self._stop.is_set():
            display_code = diagnostic_sign_code(code)
            self._safe_notify(
                f"签到等待验证, 码 {display_code}",
                f"请查看签到浏览器；如有验证码，请手动完成。软件会继续核对签到结果 (码 {display_code})。")

    def _notify_result(self, result: SignResult) -> None:
        if self._stop.is_set():
            return
        code = diagnostic_sign_code(result.sign_code)
        if result.status == "success":
            self._safe_notify(f"自动签到成功, 码 {code}", f"✅ 已自动完成签到 (码 {code})")
        elif result.status == "failure":
            self._safe_notify(f"自动签到失败, 码 {code}", f"❌ 签到失败 (码 {code})\n{result.message}")
        else:
            self._safe_notify(f"自动签到结果未知, 码 {code}", f"⚠️ 请人工确认: {result.message}")

    def _safe_notify(self, reason: str, text: str) -> None:
        """Best-effort notification that can never fail the sign-in worker."""
        try:
            self.notify(reason, text)
        except Exception as exc:
            # Notifications are auxiliary (for example, WeChat may be
            # unavailable).  Preserve the actual browser/result flow and keep
            # a local diagnostic for operators.
            print(f"[warn] 签到通知失败：{type(exc).__name__}")


class _BrowserSession:
    """One isolated browser per monitor, with atomic request/result files.

    File exchange also works with windowed frozen executables, which may not
    have stdin/stdout. The parent remains able to stop the entire child tree.
    """

    def __init__(self, stop: threading.Event, timeout_s: float, *,
                 on_verification=None, captcha_boost: bool = True):
        self.stop = stop
        self.timeout_s = timeout_s
        self.resources = ExitStack()
        self.directory: Path | None = None
        self.proc = None
        self.sequence = 0
        self.in_request = False
        self.on_verification = on_verification
        self.captcha_boost = captcha_boost

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _wait(self, path: Path, *, progress=None) -> None:
        deadline = time.monotonic() + self.timeout_s
        while not path.exists():
            if self.stop.is_set():
                raise InterruptedError("监控已停止")
            if self.proc.poll() is not None:
                raise RuntimeError("浏览器进程已退出")
            if progress is not None:
                progress()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired("prepared browser", self.timeout_s)
            self.stop.wait(min(0.05, remaining))
        if self.stop.is_set():
            raise InterruptedError("监控已停止")

    def open(self) -> None:
        if self.stop.is_set():
            raise InterruptedError("监控已停止")
        self.directory = Path(self.resources.enter_context(
            tempfile.TemporaryDirectory(prefix="hdusign-browser-")))
        log = self.resources.enter_context(tempfile.TemporaryFile())
        command = browser_command("--sign-worker", "--exchange-dir", str(self.directory))
        if not self.captcha_boost:
            command.append("--captcha-boost=off")
        self.proc = self.resources.enter_context(subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, cwd=str(ROOT),
            **hidden_subprocess_options()))
        ready = self.directory / "ready.json"
        self._wait(ready)
        result = json.loads(ready.read_text(encoding="utf-8"))
        if result.get("status") != "ready":
            raise RuntimeError(result.get("message", "签到页未就绪"))

    def submit(self, code: str, detected_at: float) -> SignResult:
        if self.stop.is_set():
            raise InterruptedError("监控已停止")
        self.sequence += 1
        request = self.sequence
        self.in_request = True
        request_finished = False
        try:
            # Never replace a file the Windows child may still have open for reading.
            # Immutable request names also prevent reprocessing an earlier code.
            write_json(self.directory / f"request-{request}.json", {
                "id": request, "code": code, "detected_at": detected_at})
            result_path = self.directory / f"result-{request}.json"
            self._wait(result_path, progress=verification_progress(
                self.directory / f"progress-{request}.json", code, self.on_verification))
            result = SignResult.read(result_path, code)
            # Once a well-formed result has been read, this request is complete
            # and the prepared browser may be reused for the next code.  Any
            # timeout, write failure, or malformed result before this point
            # leaves the child in an unknown state and must abort it immediately.
            request_finished = True
            if result.status == "success" and self.proc.poll() not in (None, 0):
                return SignResult("unknown", code, "浏览器异常退出，签到结果需到平台核对")
            timing = self.directory / f"timing-{request}.json"
            try:
                if timing.exists():
                    values = json.loads(timing.read_text(encoding="utf-8"))
                    if "page_ready" in values and "submit" in values:
                        ready_s = max(0.0, values["page_ready"] - detected_at)
                        submit_s = max(0.0, values["submit"] - detected_at)
                        print(f"[autosign] 确认码→页面就绪 {ready_s:.2f}s，确认码→点击签到 {submit_s:.2f}s")
            except (OSError, ValueError, TypeError):
                print("[warn] 无法读取操作耗时；签到结果已单独确认")
            return result
        finally:
            if request_finished or self.proc is None:
                self.in_request = False
            else:
                try:
                    if self.proc.poll() is None:
                        # Do not let close() take its 1.5s graceful-stop path
                        # after a failed request: the browser may still be
                        # submitting it.
                        AutoSigner._terminate_child(self.proc)
                except Exception as exc:
                    print(f"[warn] 失败签到浏览器清理异常：{exc}")
                    # Keep the in-flight marker so close() also takes the
                    # immediate termination path if cleanup itself failed.
                else:
                    self.in_request = False

    def close(self) -> None:
        try:
            if self.proc is not None and self.proc.poll() is None:
                if self.in_request:
                    AutoSigner._terminate_child(self.proc)
                else:
                    try:
                        if self.directory is not None:
                            write_json(self.directory / "stop.json", {})
                        self.proc.wait(timeout=1.5)
                    except (OSError, subprocess.TimeoutExpired):
                        AutoSigner._terminate_child(self.proc)
        finally:
            self.resources.close()
            self.proc = None


def make_auto_signer(cfg: dict, alerter, stop: threading.Event | None = None):
    options = cfg.get("auto_sign") or {}
    if not isinstance(options, dict) or not options.get("enabled"):
        return None

    def notify(reason: str, text: str):
        print(f"!!!! {reason} !!!!")
        alerter.notify(text, "code", reason)

    timeout = _timeout_seconds(options.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
    return AutoSigner(notify, timeout, stop,
                      prewarm_browser=bool(options.get("prewarm_browser", True)),
                      captcha_boost=bool(options.get("captcha_trajectory", True)))


def _timeout_seconds(value: object) -> int:
    """Return a bounded timeout, falling back for malformed config values."""
    try:
        if isinstance(value, bool):
            raise ValueError
        timeout = float(value)
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_TIMEOUT_SECONDS
    if (not math.isfinite(timeout)
            or not MIN_TIMEOUT_SECONDS <= timeout <= MAX_TIMEOUT_SECONDS):
        return DEFAULT_TIMEOUT_SECONDS
    return int(timeout)
