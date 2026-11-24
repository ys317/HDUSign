"""Receive a live stream's audio without displaying or decoding its video."""
from __future__ import annotations

import json
import math
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Iterator
from urllib.parse import urlsplit

import numpy as np

from hdusign.audio import SAMPLE_RATE
from hdusign.processes import hidden_subprocess_options
from hdusign.runtime import FFMPEG_BINARY, resource_root


# Absorb a network catch-up burst or a slow recognizer without losing audio.
# ASR normally runs several times faster than real time, so a backlog this
# long drains within a few seconds.
_BUFFER_SECONDS = 10.0
_START_TIMEOUT_SECONDS = 30.0
_IDLE_TIMEOUT_SECONDS = 20.0
_IO_TIMEOUT_SECONDS = 15.0
_STOP_GRACE_SECONDS = 0.75
_THREAD_JOIN_SECONDS = 1.0
_POLL_SECONDS = 0.05
_HEADER_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+\Z")
_HTTP_AUTH_ERROR = re.compile(rb"\b(?:http error|server returned|http/[12](?:\.[01])?)\s+(?:401|403)\b")
_TLS_ERRORS = (b"certificate verify failed", b"certificate verification failed",
               b"certificate verification error", b"peer certificate cannot be authenticated",
               b"certificate is not trusted", b"certificate is not yet activated",
               b"certificate has expired", b"certificate revoked", b"certificate has been revoked",
               b"certificate's owner does not match", b"certificate does not match",
               b"sni or certificate check failed",
               # Schannel reports Windows certificate/identity failures as HRESULTs.
               b"0x80090322", b"0x80090325", b"0x80090327", b"0x80090328",
               b"0x80092012", b"0x80092013", b"0x800b0101", b"0x800b0109",
               b"0x800b010c", b"0x800b010f")
_UNSUPPORTED_FORMAT = "该直播格式暂不支持后台音频，请切回系统声音监听。"


class MediaError(RuntimeError):
    """An actionable error safe to print without disclosing stream credentials."""

    def __init__(self, message: str, *, kind: str = "transient"):
        super().__init__(message)
        self.kind = kind

    @property
    def retryable(self) -> bool:
        return self.kind in {"transient", "auth", "no_audio"}


def find_ffmpeg() -> str:
    """Portable packages never depend on a separately installed FFmpeg."""
    bundled = resource_root() / "ffmpeg" / FFMPEG_BINARY
    if bundled.is_file():
        return str(bundled)
    if getattr(sys, "frozen", False):
        raise MediaError("直播音频组件缺失，请重新完整解压 HDUSign 发行包。", kind="terminal")
    installed = shutil.which("ffmpeg")
    if installed:
        return installed
    raise MediaError("未找到 FFmpeg 音频组件，请使用完整的 HDUSign 发行包或安装 FFmpeg。", kind="terminal")


def _checked_headers(headers: dict | None) -> dict[str, str]:
    if headers is None:
        return {}
    if not isinstance(headers, dict):
        raise MediaError("直播请求头格式无效，请重新打开直播页面。", kind="terminal")
    result = {}
    for name, value in headers.items():
        if (not isinstance(name, str) or not _HEADER_NAME.fullmatch(name)
                or not isinstance(value, str)
                or any(ord(char) < 32 or ord(char) == 127 for char in value)):
            raise MediaError("直播请求头格式无效，请重新打开直播页面。", kind="terminal")
        result[name] = value
    return result


def _tls_options(executable: str) -> list[str]:
    """Use the bundled decoder's Windows trust store, or an external CA bundle."""
    try:
        metadata = json.loads(Path(executable).with_name("SOURCE.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        metadata = {}
    # Schannel validates both trust and peer identity with Windows. Its FFmpeg
    # backend does not consume ca_file; all backends still require verification.
    if isinstance(metadata, dict) and metadata.get("tls_backend") == "schannel":
        return ["-tls_verify", "1"]
    import certifi

    certificate = certifi.where()
    if not Path(certificate).is_file():
        raise MediaError("直播连接证书缺失，请重新完整解压 HDUSign 发行包。", kind="terminal")
    return ["-tls_verify", "1", "-ca_file", certificate]


class FFmpegAudioSource:
    """Yield paced 16 kHz mono float32 chunks from one resolved live stream.

    The caller resolves a direct HTTP(S)-FLV URL and any authorized headers. No
    player, browser, login, stream switching, or reconnect is performed here.
    HLS is rejected: FFmpeg does not propagate TLS verification to HLS segments.
    Local media files are also accepted for offline checks. Set ``stop`` or close
    the generator to release the decoder, including during a network stall.

    When the consumer falls a whole buffer behind, the stale backlog is dropped
    and the stream continues from live audio. ``discontinuities`` and
    ``skipped_seconds`` grow before the first chunk after such a gap is yielded
    so the caller can reset recognition instead of joining audio across it.
    """

    def __init__(self, url: str, headers: dict | None = None,
                 chunk_seconds: float = 0.25, executable: str | None = None):
        if (not isinstance(url, str) or not url.strip()
                or any(ord(char) < 32 or ord(char) == 127 for char in url)):
            raise MediaError("直播音频地址无效，请重新打开直播页面。", kind="terminal")
        if (isinstance(chunk_seconds, bool) or not isinstance(chunk_seconds, (int, float))
                or not math.isfinite(chunk_seconds) or not 0.01 <= chunk_seconds <= 2.0):
            raise MediaError("音频分块时长必须在 0.01 到 2 秒之间。", kind="terminal")
        self.remote = url.lower().startswith(("http://", "https://"))
        if not self.remote and not Path(url).is_file():
            raise MediaError("直播音频地址无效，请重新打开直播页面。", kind="terminal")
        self.url = url
        self.headers = _checked_headers(headers)
        self.chunk_frames = int(SAMPLE_RATE * chunk_seconds)
        self.executable = executable
        self.discontinuities = 0
        self.skipped_seconds = 0.0

    def _command(self, executable: str) -> list[str]:
        command = [str(executable), "-hide_banner", "-loglevel", "error", "-nostats",
                   "-nostdin", "-filter_threads", "1", "-threads", "1"]
        # Read at network speed. Rate limiting (-re) never paces a catch-up
        # burst after a stall, but it does sleep across a server timestamp jump
        # until the idle deadline fires; the bounded queue absorbs bursts instead.
        command += ["-probesize", "1048576", "-analyzeduration", "2000000"]
        if self.remote:
            invalid_url = False
            try:
                parsed = urlsplit(self.url)
            except ValueError:
                invalid_url = True
            if invalid_url:
                raise MediaError("直播音频地址无效，请重新打开直播页面。", kind="terminal")
            if parsed.path.lower().endswith((".m3u8", ".m3u")):
                raise MediaError(_UNSUPPORTED_FORMAT, kind="terminal")
            command += ["-rw_timeout", str(int(_IO_TIMEOUT_SECONDS * 1_000_000)),
                        "-format_whitelist", "flv"]
            if parsed.scheme.lower() == "https":
                command += ["-protocol_whitelist", "https,tcp,tls,httpproxy",
                            *_tls_options(executable)]
            else:
                # TLS-only options are rejected as unused on plain HTTP input.
                # Block HTTPS redirects here instead of silently using FFmpeg's
                # default (unverified) TLS. Re-resolving can supply an HTTPS URL.
                command += ["-protocol_whitelist", "http,tcp,httpproxy"]
            if self.headers:
                command += ["-headers", "".join(f"{key}: {value}\r\n"
                                               for key, value in self.headers.items())]
        else:
            command += ["-protocol_whitelist", "file,pipe", "-format_whitelist",
                        "wav,matroska,webm,mov,mp3,aac,flac,ogg,flv"]
        command += ["-vn", "-sn", "-dn", "-i", self.url, "-map", "0:a:0",
                    "-vn", "-sn", "-dn", "-c:a", "pcm_f32le", "-threads", "1",
                    "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "f32le",
                    "-flush_packets", "1", "pipe:1"]
        return command

    def chunks(self, stop: threading.Event | None = None) -> Iterator[np.ndarray]:
        if stop is not None and stop.is_set():
            return
        command = self._command(self.executable or find_ffmpeg())
        process = None
        try:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       bufsize=0, **hidden_subprocess_options())
        except (OSError, ValueError):
            # Do not retain CalledProcessError/TimeoutExpired or a chained
            # exception: those can contain the signed URL and Cookie arguments.
            pass
        finally:
            command.clear()
        if process is None:
            raise MediaError("无法启动直播音频组件，请重新完整解压 HDUSign 发行包。", kind="terminal")

        decoder = _Decoder(process, self.chunk_frames, stop)
        try:
            decoder.start()
            while not decoder.stopping():
                item = None
                try:
                    item = decoder.buffer.get(timeout=_POLL_SECONDS)
                except queue.Empty:
                    pass
                if item is None:
                    if decoder.finished.is_set():
                        break
                    continue
                samples, dropped_frames = item
                if dropped_frames:
                    self.discontinuities += 1
                    self.skipped_seconds += dropped_frames / SAMPLE_RATE
                if not decoder.stopping():
                    yield samples
        finally:
            decoder.close()
        # Internal shutdown is a failed stream, not normal EOF. The caller must
        # reset ASR before reconnecting so fragments on either side of missing
        # audio cannot be joined into a sign-in code.
        if decoder.error and (stop is None or not stop.is_set()):
            raise MediaError(decoder.error, kind=decoder.error_kind)


class _Decoder:
    """Drain both pipes and independently supervise cancellation and deadlines."""

    def __init__(self, process, chunk_frames: int, stop: threading.Event | None):
        self.process = process
        self.chunk_bytes = chunk_frames * 4
        capacity = max(1, math.ceil(_BUFFER_SECONDS * SAMPLE_RATE / chunk_frames))
        # Items are (samples, frames dropped immediately before these samples).
        self.buffer: queue.Queue[tuple[np.ndarray, int]] = queue.Queue(maxsize=capacity)
        self.stop = stop
        self.shutdown = threading.Event()
        self.audio_done = threading.Event()
        self.finished = threading.Event()
        self.error = ""
        self.error_kind = "transient"
        self.no_audio = False
        self.unsupported_format = False
        self.auth_failed = False
        self.tls_failed = False
        self.received = False
        self.last_data = time.monotonic()
        self.reader = threading.Thread(target=self._read_audio,
                                       name="HDUSign live audio", daemon=True)
        self.stderr_reader = threading.Thread(target=self._read_stderr,
                                              name="HDUSign media errors", daemon=True)
        self.supervisor = threading.Thread(target=self._supervise,
                                           name="HDUSign media supervisor", daemon=True)

    def stopping(self) -> bool:
        return self.shutdown.is_set() or (self.stop is not None and self.stop.is_set())

    def start(self) -> None:
        self.reader.start()
        self.stderr_reader.start()
        self.supervisor.start()

    def _fail(self, message: str, kind: str = "transient") -> None:
        if self.error and self.error_kind == "terminal" and kind != "terminal":
            return
        self.error = message
        self.error_kind = kind

    def _enqueue(self, raw: bytes) -> None:
        samples = np.frombuffer(raw, dtype="<f4").astype(np.float32, copy=False)
        dropped = 0
        while True:
            try:
                self.buffer.put_nowait((samples, dropped))
                return
            except queue.Full:
                pass
            # The recognizer is a whole buffer behind. Skip the stale backlog and
            # continue from live audio rather than aborting the connection. The
            # gap is reported with the next chunk so the consumer resets ASR and
            # unrelated digit fragments are never spliced together. Only this
            # thread adds items, so the put after draining cannot fail again.
            while True:
                try:
                    stale, stale_dropped = self.buffer.get_nowait()
                except queue.Empty:
                    break
                dropped += len(stale) + stale_dropped

    def _read_audio(self) -> None:
        pending = bytearray()
        try:
            while not self.stopping():
                raw = self.process.stdout.read(self.chunk_bytes - len(pending))
                if not raw:
                    break
                self.received = True
                self.last_data = time.monotonic()
                pending.extend(raw)
                if len(pending) == self.chunk_bytes:
                    self._enqueue(bytes(pending))
                    pending.clear()
            if pending and not self.stopping():
                if len(pending) % 4:
                    self._fail("直播音频数据不完整，请重新启动监听。")
                else:
                    self._enqueue(bytes(pending))
        except (OSError, ValueError):
            if not self.stopping():
                self._fail("直播音频读取失败，请重新启动监听。")
        finally:
            self.audio_done.set()

    def _read_stderr(self) -> None:
        # Only classify known failures. Raw FFmpeg logs can contain credentials
        # and must never be included in errors or retained as an unbounded log.
        tail = b""
        try:
            while True:
                raw = self.process.stderr.read(1024)
                if not raw:
                    break
                message = (tail + raw).lower()
                if (b"matches no streams" in message
                        or b"does not contain any stream" in message):
                    self.no_audio = True
                if (b"format not on whitelist" in message
                        or b"not detecting m3u8/hls" in message):
                    self.unsupported_format = True
                if _HTTP_AUTH_ERROR.search(message):
                    self.auth_failed = True
                if any(pattern in message for pattern in _TLS_ERRORS):
                    self.tls_failed = True
                tail = message[-128:]
        except (OSError, ValueError):
            pass

    def _terminate(self) -> None:
        if self.process.poll() is not None:
            return
        try:
            self.process.terminate()
            self.process.wait(timeout=_STOP_GRACE_SECONDS)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            self.process.kill()
            self.process.wait(timeout=_STOP_GRACE_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            self._fail("直播音频组件未响应停止，请重新启动 HDUSign。", "terminal")

    def _supervise(self) -> None:
        code = None
        try:
            while not self.stopping():
                if self.audio_done.is_set():
                    try:
                        code = self.process.wait(timeout=_STOP_GRACE_SECONDS)
                    except subprocess.TimeoutExpired:
                        self._fail("直播音频组件未正常结束，请重新启动监听。", "terminal")
                    break
                timeout = _IDLE_TIMEOUT_SECONDS if self.received else _START_TIMEOUT_SECONDS
                if time.monotonic() - self.last_data >= timeout:
                    self._fail("直播音频接收超时，请确认正在直播并检查网络后重试。")
                    break
                self.shutdown.wait(_POLL_SECONDS)
        finally:
            self._terminate()
            # Killing the process closes its pipe ends, releasing a blocking
            # read on Windows. Closing a buffered pipe first can deadlock.
            for worker in (self.reader, self.stderr_reader):
                worker.join(timeout=_THREAD_JOIN_SECONDS)
            for pipe in (self.process.stdout, self.process.stderr):
                try:
                    pipe.close()
                except (OSError, ValueError):
                    self._fail("直播音频组件未响应停止，请重新启动 HDUSign。", "terminal")
            if self.reader.is_alive() or self.stderr_reader.is_alive():
                self._fail("直播音频组件未响应停止，请重新启动 HDUSign。", "terminal")
            # Classify only after both readers have been joined. In particular,
            # a final HTTP/TLS error must not arrive after a generic exit error
            # has already been published to the monitor.
            if not self.stopping():
                if self.tls_failed and self.error_kind != "terminal":
                    self._fail("直播服务器证书校验失败，请检查系统时间或联系直播平台。", "terminal")
                elif not self.error:
                    if self.unsupported_format:
                        self._fail(_UNSUPPORTED_FORMAT, "terminal")
                    elif self.auth_failed:
                        self._fail("直播音频凭据已失效或访问被拒绝，需要刷新直播连接。", "auth")
                    elif self.no_audio or (code == 0 and not self.received):
                        self._fail("直播没有可用音轨，请确认直播已开始且包含声音。", "no_audio")
                    elif code is not None and code != 0:
                        self._fail("直播音频连接失败或已中断，请检查网络和登录状态后重试。")
            self.finished.set()

    def close(self) -> None:
        self.shutdown.set()
        if self.supervisor.ident is None:
            # Also release the process if starting a worker failed.
            self._terminate()
            for worker in (self.reader, self.stderr_reader):
                if worker.ident is not None:
                    worker.join(timeout=_THREAD_JOIN_SECONDS)
            for pipe in (self.process.stdout, self.process.stderr):
                try:
                    pipe.close()
                except (OSError, ValueError):
                    self._fail("直播音频组件未响应停止，请重新启动 HDUSign。", "terminal")
        else:
            self.supervisor.join(timeout=2 * _STOP_GRACE_SECONDS + 3 * _THREAD_JOIN_SECONDS + 1)
            if self.supervisor.is_alive():
                raise MediaError("直播音频组件未响应停止，请重新启动 HDUSign。", kind="terminal")
