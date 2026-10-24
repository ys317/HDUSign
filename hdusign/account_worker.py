"""Private worker protocol: explicit account root, immutable config and child env."""
from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

from hdusign.accounts import SESSION_ENV, session_credentials, write_json
from hdusign.processes import hidden_subprocess_options
from hdusign.runtime import application_root


def _open_log_pair(path):
    output = path.open("wb")
    try:
        reader = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        output.close()
        raise
    return output, reader


def worker_main(argv):
    directory = Path(argv[0]).resolve()
    request = json.loads((directory / "request.json").read_text("utf-8"))
    root = Path(os.environ["HDUSIGN_ACCOUNT_ROOT"]).resolve()
    if root != Path(request["root"]).resolve():
        return 2
    os.chdir(root)
    stop = threading.Event()
    parent_pid = request["parent_pid"]

    def watch():
        # A crashed parent must not leave monitors/live browsers running.
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x100000, False, parent_pid)
        try:
            while not stop.wait(.1):
                if ((directory / "stop").exists() or not handle
                        or kernel.WaitForSingleObject(handle, 0) == 0):
                    stop.set()
        finally:
            if handle:
                kernel.CloseHandle(handle)

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    journal = None
    outputs = []
    outcome = "failed"
    log_directory = request.get("run_log_dir")
    if log_directory:
        from hdusign.diagnostics import Journal, JournalOutput
        log_directory = Path(log_directory).resolve()
        if not log_directory.is_relative_to((root / "logs").resolve()):
            stop.set()
            return 2
        try:
            journal = Journal(log_directory)
            for name in ("stdout", "stderr"):
                original = getattr(sys, name)
                wrapper = JournalOutput(original, journal, name)
                outputs.append((name, original, wrapper))
                setattr(sys, name, wrapper)
            journal.record("task_requested", at=request.get("queued_at", time.time()),
                           parent_pid=parent_pid, action=request["action"])
            journal.record("task_started", action=request["action"], pid=os.getpid())
        except OSError:
            print("[warn] 完整日志创建失败，本次继续执行任务。", flush=True)
    try:
        from hdusign.desktop_controller import Services
        from hdusign.accounts import load_credentials
        services = Services()
        cfg, action = dict(request["config"]), request["action"]
        if journal:
            cfg["_run_journal"] = journal
        if action in ("monitor", "courses"):
            from hdusign.live import LiveLoginRequired
            try:
                saved = json.loads((root / "live_session.json").read_text("utf-8-sig"))
            except (OSError, ValueError):
                raise LiveLoginRequired("请先登录当前账号，再读取课程或开始监听。") from None
            expected = str(load_credentials(root).get("skl_username") or "").strip()
            if not expected or saved.get("user_code", saved.get("username")) != expected:
                raise LiveLoginRequired("登录状态与当前账号不一致，请重新登录当前账号。")
        if action == "login":
            value = services.login(request["url"], stop, load_credentials(root), request["manual"])
        elif action in ("monitor", "courses", "webhook"):
            value = getattr(services, action)(cfg, stop)
            if action == "courses":
                value = [asdict(item) for item in value]
        elif action == "isolation-check":
            # Offline diagnostics, also used to verify five concurrent workers.
            credentials = load_credentials(root)
            saved = json.loads((root / "live_session.json").read_text("utf-8"))
            from hdusign import browser, live
            if request.get("check_model"):
                import numpy as np
                from hdusign.monitor import make_engine
                engine = make_engine(cfg)
                engine.accept(np.zeros(4000, dtype=np.float32))
                engine.flush()
            value = {"root": str(root), "username": credentials.get("skl_username"),
                     "session_username": saved.get("username"), "pid": os.getpid(),
                     "live_root": str(live.application_root()),
                     "browser_root": str(browser.ROOT), "legacy_profile": str(browser.account_profile(credentials))}
            if request.get("check_notification_route"):
                from hdusign.reservation_webhooks import route_resolver, webhook_key
                resolver = route_resolver(root, cfg["_notification_route"])
                value["notification_key_before"] = webhook_key(resolver())
            if request.get("check_diagnostics"):
                from hdusign.diagnostics import Diagnostics
                import numpy as np
                clock = [time.time()]
                if journal:
                    journal.record("offline_diagnostic_fixture", synthetic_audio=True)
                diagnostic = Diagnostics(cfg, journal=journal, wall=lambda: clock[0])
                diagnostic.begin_stream("offline-fixture")
                clock[0] += 1
                level = max(-1.0, min(1.0, float(request.get("fixture_sample_value", 0))))
                samples = np.full(16000, level, dtype=np.float32)
                diagnostic.audio(samples)
                diagnostic.asr_result(samples, .01, "离线诊断测试", [])
                diagnostic.final("离线诊断测试")
                diagnostic.close()
                print("[i] 离线诊断日志测试：未连接学校，未执行语音识别或签到。")
                value["diagnostics_checked"] = True
            # Intentionally no explicit flush: the entry point must make this
            # visible while the worker is idle and has not produced ASR text.
            print("[i] 离线账号隔离自检已就绪。")
            if request.get("hold"):
                (directory / "ready").touch()
                while not stop.wait(.05) and not (directory / "release").exists():
                    pass
            if request.get("check_notification_route"):
                value["notification_key_after"] = webhook_key(resolver())
        else:
            raise ValueError("未知账号任务")
        write_json(directory / "result.json", {"ok": True, "value": value})
        outcome = "stopped" if stop.is_set() else "completed"
        return 0
    except Exception as exc:
        # Never return requests, signed URLs, cookies, credentials or tracebacks.
        from hdusign.live import LiveError
        message = str(exc) if isinstance(exc, LiveError) else "账号任务失败，请检查网络、登录状态和运行环境。"
        print("[错误] " + message, flush=True)
        write_json(directory / "result.json", {"ok": False, "message": message})
        return 1
    finally:
        stop.set()
        watcher.join(1)
        if journal:
            journal.record("task_finished", outcome=outcome)
        for name, original, wrapper in outputs:
            wrapper.finish()
            setattr(sys, name, original)
        if journal:
            journal.close()


class ProcessServices:
    isolated = True

    def __init__(self, root, log=None):
        self.root = Path(root).resolve()
        self.credentials = {}
        self.log = log or (lambda line: None)
        self.process = None
        self.job_directory = None
        self.last_log_directory = None

    def environment(self, credentials):
        env = dict(os.environ)
        env.pop(SESSION_ENV, None)
        env["HDUSIGN_ACCOUNT_ROOT"] = str(self.root)
        env["PYTHONUNBUFFERED"] = "1"
        session_credentials(self.root, dict(credentials), environment=env)
        if not getattr(sys, "frozen", False):
            env["HDUSIGN_SOURCE_BOOTSTRAP"] = str(application_root() / "tools/account_worker_bootstrap.py")
        return env

    def command(self, directory):
        if getattr(sys, "frozen", False):
            return [sys.executable, "--account-worker", str(directory)]
        return [sys.executable, str(application_root() / "tools/account_worker_bootstrap.py"),
                "--account-worker", str(directory)]

    def run(self, action, cfg, stop, *, credentials=None, **extra):
        queued_at = time.time()
        credentials = dict(self.credentials if credentials is None else credentials)
        with tempfile.TemporaryDirectory(prefix="hdusign-account-job-") as temporary:
            directory = Path(temporary)
            self.job_directory = directory
            run_directory = None
            if action == "monitor" or extra.get("check_diagnostics"):
                from hdusign.diagnostics import new_run_directory
                try:
                    run_directory = new_run_directory(self.root)
                    self.last_log_directory = run_directory
                    self.log(f"[i] 本次完整日志保存在：{run_directory}")
                except OSError:
                    self.log("[warn] 完整日志目录创建失败，本次继续执行任务。")
            request = dict(action=action, config=cfg, root=str(self.root), parent_pid=os.getpid(),
                           queued_at=queued_at, **extra)
            if run_directory:
                request["run_log_dir"] = str(run_directory)
            write_json(directory / "request.json", request)
            stdout_path = (run_directory / "console.log") if run_directory else (directory / "stdout.log")
            try:
                output_file, reader_file = _open_log_pair(stdout_path)
            except OSError:
                if run_directory is None:
                    raise
                self.log("[warn] 完整日志文件无法写入，本次改用临时日志，监听继续。")
                self.last_log_directory = None
                request.pop("run_log_dir", None)
                write_json(directory / "request.json", request)
                output_file, reader_file = _open_log_pair(directory / "stdout.log")
            with output_file as output, reader_file as reader:
                with subprocess.Popen(self.command(directory), env=self.environment(credentials),
                                      cwd=self.root, stdin=subprocess.DEVNULL,
                                      stdout=output, stderr=subprocess.STDOUT,
                                      **hidden_subprocess_options()) as process:
                    self.process = process
                    stopping_at = None
                    pending = ""
                    while True:
                        pending += reader.read().replace("\r", "\n")
                        while "\n" in pending:
                            line, pending = pending.split("\n", 1)
                            if line.strip():
                                self.log(line)
                        if process.poll() is not None:
                            # Drain all output written immediately before exit.
                            for line in (pending + reader.read()).replace("\r", "\n").splitlines():
                                if line.strip():
                                    self.log(line)
                            break
                        if stop.is_set():
                            if stopping_at is None:
                                (directory / "stop").touch()
                                stopping_at = time.monotonic()
                            elif time.monotonic() - stopping_at > 10:
                                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                               capture_output=True, timeout=5,
                                               **hidden_subprocess_options())
                                process.wait(timeout=5)
                        time.sleep(.04)
                    self.process = None
            if stop.is_set():
                return [] if action == "courses" else 1 if action == "login" else None
            result_file = directory / "result.json"
            if not result_file.exists():
                raise RuntimeError("账号子进程意外退出，请检查可用内存和运行环境。")
            result = json.loads(result_file.read_text("utf-8"))
            if not result.get("ok"):
                from hdusign.live import LiveError
                raise LiveError(result.get("message", "账号任务失败"))
            value = result.get("value")
            if action == "courses":
                from hdusign.live import LiveCourse
                return [LiveCourse(**row) for row in value]
            return value

    def login(self, url, stop, credentials, manual=False):
        return self.run("login", {}, stop, credentials=credentials, url=url, manual=manual)

    def monitor(self, cfg, stop):
        return self.run("monitor", cfg, stop)

    def courses(self, cfg, stop):
        return self.run("courses", cfg, stop)



    def webhook(self, cfg, stop):
        return self.run("webhook", cfg, stop)
