"""Live-audio contracts; fake streams and local synthesized media only."""
from __future__ import annotations

import io
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import traceback
import unittest
from unittest.mock import patch
import wave

import numpy as np

from hdusign import media
from hdusign.processes import hidden_subprocess_options


class FakePipe:
    def __init__(self, data=b"", release=None, read_size=None):
        self.data = io.BytesIO(data)
        self.release = release
        self.read_size = read_size
        self.read_started = threading.Event()
        self.eof_reached = threading.Event()
        self.closed = False

    def read(self, size):
        self.read_started.set()
        if self.read_size is not None:
            size = min(size, self.read_size)
        result = self.data.read(size)
        if not result:
            self.eof_reached.set()
            if self.release is not None:
                self.release.wait(3)
        return result

    def close(self):
        self.closed = True
        if self.release is not None:
            self.release.set()


class FakeProcess:
    def __init__(self, data=b"", stderr=b"", returncode=0, stalled=False,
                 read_size=None, ignore_terminate=False):
        self.returncode = None if stalled else returncode
        self.release = threading.Event()
        blocker = self.release if stalled else None
        self.stdout = FakePipe(data, blocker, read_size)
        self.stderr = FakePipe(stderr, blocker)
        self.terminated = threading.Event()
        self.killed = False
        self.ignore_terminate = ignore_terminate
        self.args = []

    def poll(self):
        return self.returncode

    def wait(self, timeout):
        if self.returncode is None and not self.release.wait(timeout):
            raise subprocess.TimeoutExpired(self.args, timeout)
        return self.returncode

    def terminate(self):
        self.terminated.set()
        if not self.ignore_terminate:
            self.returncode = -15
            self.release.set()

    def kill(self):
        self.killed = True
        self.returncode = -9
        self.release.set()


class GatedPipe(FakePipe):
    """Deliver one chunk, then let the test resume the simulated network burst."""

    def __init__(self, data, release):
        super().__init__(data, release)
        self.resume = threading.Event()

    def read(self, size):
        if self.data.tell():
            while not self.resume.wait(0.01):
                if self.release.is_set():
                    return b""
        return super().read(size)


class MediaTests(unittest.TestCase):
    def source(self, **options):
        return media.FFmpegAudioSource("https://example.invalid/live.flv?token=secret",
                                      executable="test-ffmpeg", **options)

    def launch(self, process):
        self.command = None
        self.options = None

        def start(command, **options):
            self.command = list(command)
            self.options = dict(options)
            process.args = command
            return process

        mocked = patch.object(media.subprocess, "Popen", side_effect=start)
        mocked.start()
        self.addCleanup(mocked.stop)

    def assert_released(self, process):
        self.assertIsNotNone(process.poll())
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)
        self.assertFalse(any(thread.is_alive() and thread.name in
                             {"HDUSign live audio", "HDUSign media errors",
                              "HDUSign media supervisor"}
                             for thread in threading.enumerate()))

    def test_error_categories_have_a_read_only_recovery_contract(self):
        self.assertEqual(media.MediaError("connection ended").kind, "transient")
        for kind, retryable in (("transient", True), ("auth", True), ("no_audio", True),
                                ("terminal", False)):
            with self.subTest(kind=kind):
                error = media.MediaError("safe message", kind=kind)
                self.assertEqual(error.kind, kind)
                self.assertEqual(error.retryable, retryable)
                self.assertEqual(error.args, ("safe message",))
                with self.assertRaises(AttributeError):
                    error.retryable = not retryable

    def test_bundle_is_preferred_and_frozen_never_uses_path(self):
        with tempfile.TemporaryDirectory() as folder:
            executable = Path(folder) / "ffmpeg" / media.FFMPEG_BINARY
            executable.parent.mkdir()
            executable.write_bytes(b"not executed")
            with patch.object(media, "resource_root", return_value=Path(folder)), \
                    patch.object(media.sys, "frozen", True, create=True), \
                    patch.object(media.shutil, "which", return_value="system-ffmpeg") as which:
                self.assertEqual(media.find_ffmpeg(), str(executable))
                executable.unlink()
                with self.assertRaisesRegex(media.MediaError, "完整解压") as raised:
                    media.find_ffmpeg()
                self.assertEqual(raised.exception.kind, "terminal")
                which.assert_not_called()

    def test_development_can_use_an_installed_ffmpeg(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(media, "resource_root", return_value=Path(folder)), \
                patch.object(media.sys, "frozen", False, create=True), \
                patch.object(media.shutil, "which", return_value="system-ffmpeg"):
            self.assertEqual(media.find_ffmpeg(), "system-ffmpeg")
            with patch.object(media.shutil, "which", return_value=None):
                with self.assertRaisesRegex(media.MediaError, "未找到 FFmpeg") as raised:
                    media.find_ffmpeg()
                self.assertFalse(raised.exception.retryable)

    def test_command_only_decodes_audio_and_keeps_tls_and_windows_hidden(self):
        process = FakeProcess(np.zeros(160, dtype="<f4").tobytes())
        self.launch(process)
        headers = {"Cookie": "session=private-cookie", "Referer": "https://example.invalid/class"}
        with patch.object(media, "hidden_subprocess_options", return_value={"creationflags": 123}):
            list(self.source(headers=headers, chunk_seconds=0.01).chunks())
        command = self.command
        self.assertEqual(command[command.index("-map") + 1], "0:a:0")
        self.assertEqual(command.count("-vn"), 2)
        self.assertEqual(command[command.index("-ac") + 1], "1")
        self.assertEqual(command[command.index("-ar") + 1], "16000")
        self.assertEqual(command[command.index("-f") + 1], "f32le")
        self.assertNotIn("-re", command)
        self.assertEqual(command[command.index("-format_whitelist") + 1], "flv")
        self.assertEqual(command[command.index("-tls_verify") + 1], "1")
        self.assertTrue(Path(command[command.index("-ca_file") + 1]).is_file())
        self.assertNotIn("file", command[command.index("-protocol_whitelist") + 1].split(","))
        self.assertEqual(command[command.index("-headers") + 1],
                         "Cookie: session=private-cookie\r\nReferer: https://example.invalid/class\r\n")
        self.assertTrue(all(command[index + 1] == "1" for index, arg in enumerate(command)
                            if arg in {"-threads", "-filter_threads"}))
        self.assertEqual(self.options["creationflags"], 123)
        self.assertEqual(self.options["stdin"], subprocess.DEVNULL)
        self.assertEqual(self.options["bufsize"], 0)
        self.assertFalse(self.options.get("shell", False))
        self.assertEqual(process.args, [])
        self.assert_released(process)

    def test_plain_http_has_no_unused_tls_options_or_unverified_https_redirect(self):
        process = FakeProcess(np.zeros(160, dtype="<f4").tobytes())
        self.launch(process)
        list(media.FFmpegAudioSource("http://example.invalid/live.flv",
                                     executable="test-ffmpeg").chunks())
        self.assertNotIn("-tls_verify", self.command)
        self.assertNotIn("-ca_file", self.command)
        protocols = self.command[self.command.index("-protocol_whitelist") + 1].split(",")
        self.assertNotIn("https", protocols)
        self.assertNotIn("tls", protocols)
        self.assertIn("http", protocols)
        self.assert_released(process)

    def test_schannel_uses_windows_trust_without_an_unused_ca_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "ffmpeg.exe"
            (executable.parent / "SOURCE.json").write_text(
                '{"component": "FFmpeg", "tls_backend": "schannel"}', encoding="utf-8")
            source = media.FFmpegAudioSource("https://example.invalid/live.flv",
                                             executable=str(executable))
            with patch("certifi.where", side_effect=AssertionError("Schannel uses Windows trust")):
                command = source._command(str(executable))
            self.assertEqual(command[command.index("-tls_verify") + 1], "1")
            self.assertNotIn("-ca_file", command)
            self.assertEqual(command[command.index("-protocol_whitelist") + 1],
                             "https,tcp,tls,httpproxy")

    def test_hls_is_rejected_before_starting_a_process(self):
        with patch.object(media.subprocess, "Popen") as process:
            for suffix in (".m3u8", ".m3u", ".M3U8?token=private"):
                with self.assertRaisesRegex(media.MediaError, "格式暂不支持后台音频") as raised:
                    list(media.FFmpegAudioSource("https://example.invalid/live" + suffix,
                                                 executable="test-ffmpeg").chunks())
                self.assertEqual(raised.exception.kind, "terminal")
            process.assert_not_called()

    def test_headers_and_addresses_cannot_inject_line_breaks(self):
        invalid_headers = [{"Cookie\r\nInjected": "value"}, {"Cookie": "secret\r\nInjected: yes"},
                           {"Cookie": "secret\x00"}, {"Bad Name": "value"},
                           {"Cookie": 123}, {1: "value"}, [], "Cookie: secret"]
        with patch.object(media.subprocess, "Popen") as process:
            for headers in invalid_headers:
                with self.subTest(headers=type(headers).__name__):
                    with self.assertRaisesRegex(media.MediaError, "请求头格式无效") as raised:
                        self.source(headers=headers)
                    self.assertFalse(raised.exception.retryable)
            for url in ("", None, "https://example.invalid/\r\nsecret", "https://example.invalid/\x00"):
                with self.assertRaisesRegex(media.MediaError, "地址无效") as raised:
                    media.FFmpegAudioSource(url)
                self.assertFalse(raised.exception.retryable)
            process.assert_not_called()

    def test_invalid_chunk_duration_cannot_allocate_unbounded_buffers(self):
        for duration in (0, -1, 100, True, None, "0.25", float("inf"), float("nan")):
            with self.subTest(duration=duration), self.assertRaisesRegex(media.MediaError, "分块时长") as raised:
                self.source(chunk_seconds=duration)
            self.assertEqual(raised.exception.kind, "terminal")

    def test_short_pipe_reads_are_reassembled_and_final_partial_is_preserved(self):
        expected = np.arange(641, dtype=np.float32) / 1000
        process = FakeProcess(expected.astype("<f4").tobytes(), read_size=3)
        self.launch(process)
        chunks = list(self.source(chunk_seconds=0.01).chunks())
        self.assertEqual([len(chunk) for chunk in chunks], [160, 160, 160, 160, 1])
        self.assertTrue(all(chunk.dtype == np.float32 and chunk.ndim == 1 for chunk in chunks))
        self.assertTrue(np.array_equal(np.concatenate(chunks), expected))
        self.assert_released(process)

    def test_no_audio_stream_and_empty_success_are_actionable(self):
        for stderr, returncode in ((b"Stream map '0:a:0' matches no streams", 1), (b"", 0)):
            with self.subTest(returncode=returncode):
                process = FakeProcess(stderr=stderr, returncode=returncode)
                with patch.object(media.subprocess, "Popen", return_value=process):
                    with self.assertRaisesRegex(media.MediaError, "没有可用音轨") as raised:
                        list(self.source().chunks())
                self.assertEqual(raised.exception.kind, "no_audio")
                self.assertTrue(raised.exception.retryable)
                self.assert_released(process)

    def test_nonzero_exit_is_reported_after_already_buffered_audio(self):
        process = FakeProcess(np.zeros(160, dtype="<f4").tobytes(),
                              stderr=b"https://example.invalid/?token=secret Cookie=private", returncode=1)
        self.launch(process)
        chunks = self.source(chunk_seconds=0.01).chunks()
        self.assertEqual(len(next(chunks)), 160)
        with self.assertRaisesRegex(media.MediaError, "连接失败或已中断") as raised:
            next(chunks)
        error = raised.exception
        self.assertEqual(error.kind, "transient")
        rendered = "".join(traceback.format_exception(error))
        self.assertNotIn("secret", rendered)
        self.assertNotIn("private", rendered)
        self.assertIsNone(error.__context__)
        self.assertIsNone(error.__cause__)
        self.assertFalse(hasattr(error, "cmd"))
        self.assert_released(process)

    def test_http_auth_errors_can_refresh_without_exposing_signed_urls(self):
        for status in (401, 403):
            with self.subTest(status=status):
                stderr = (b"x" * 3000 + f"\nHTTP error {status} denied\n".encode()
                          + b"https://example.invalid/live?token=private Cookie=secret")
                process = FakeProcess(np.zeros(160, dtype="<f4").tobytes(),
                                      stderr=stderr, returncode=1)
                process.stderr.read_size = 9
                with patch.object(media.subprocess, "Popen", return_value=process):
                    with self.assertRaisesRegex(media.MediaError, "凭据已失效或访问被拒绝") as raised:
                        list(self.source(chunk_seconds=0.01).chunks())
                error = raised.exception
                self.assertEqual(error.kind, "auth")
                self.assertTrue(error.retryable)
                self.assertIsNone(error.__context__)
                self.assertIsNone(error.__cause__)
                rendered = "".join(traceback.format_exception(error))
                self.assertNotIn("private", rendered)
                self.assertNotIn("secret", rendered)
                self.assert_released(process)

    def test_explicit_certificate_failures_are_terminal(self):
        errors = (b"SSL routines::certificate verify failed",
                  b"Peer certificate cannot be authenticated with given CA certificates",
                  b"The certificate is NOT trusted. The certificate issuer is unknown.",
                  b"The certificate has been revoked.",
                  b"The name in the certificate does not match the expected.",
                  b"HTTP error 403 denied\ncertificate verification failed",
                  b"schannel: next InitializeSecurityContext failed: (0x80090325)",
                  b"schannel: next InitializeSecurityContext failed: (0x80090322)",
                  b"schannel: next InitializeSecurityContext failed: (0x80090328)",
                  b"[tls] SNI or certificate check failed")
        for stderr in errors:
            with self.subTest(error=stderr.split(b" ")[0]):
                process = FakeProcess(stderr=stderr + b" https://example.invalid/?token=private", returncode=1)
                process.stderr.read_size = 9
                with patch.object(media.subprocess, "Popen", return_value=process):
                    with self.assertRaisesRegex(media.MediaError, "证书校验失败") as raised:
                        list(self.source().chunks())
                error = raised.exception
                self.assertEqual(error.kind, "terminal")
                self.assertFalse(error.retryable)
                self.assertIsNone(error.__context__)
                self.assertNotIn("private", "".join(traceback.format_exception(error)))
                self.assert_released(process)

    def test_user_stop_suppresses_pending_auth_and_certificate_errors(self):
        for stderr in (b"Server returned 401 Unauthorized", b"certificate verify failed"):
            with self.subTest(error=stderr.split(b" ")[0]):
                process = FakeProcess(np.zeros(160, dtype="<f4").tobytes(), stderr=stderr, stalled=True)
                stop = threading.Event()
                with patch.object(media.subprocess, "Popen", return_value=process):
                    chunks = self.source(chunk_seconds=0.01).chunks(stop)
                    try:
                        next(chunks)
                        self.assertTrue(process.stderr.eof_reached.wait(1))
                        stop.set()
                        self.assertEqual(list(chunks), [])
                    finally:
                        chunks.close()
                self.assert_released(process)

    def test_cleanup_failure_is_terminal_and_does_not_chain_raw_errors(self):
        process = FakeProcess(np.zeros(160, dtype="<f4").tobytes())
        self.launch(process)
        with patch.object(process.stdout, "close", side_effect=OSError("private secret")):
            with self.assertRaisesRegex(media.MediaError, "未响应停止") as raised:
                list(self.source(chunk_seconds=0.01).chunks())
        error = raised.exception
        self.assertEqual(error.kind, "terminal")
        self.assertFalse(error.retryable)
        self.assertIsNone(error.__context__)
        self.assertIsNone(error.__cause__)
        self.assertNotIn("private", "".join(traceback.format_exception(error)))
        process.stdout.close()
        self.assert_released(process)

    def test_start_failure_has_no_secret_bearing_exception_chain(self):
        with patch.object(media.subprocess, "Popen", side_effect=OSError("private-cookie secret-token")):
            with self.assertRaisesRegex(media.MediaError, "无法启动直播音频组件") as raised:
                list(self.source().chunks())
        self.assertIsNone(raised.exception.__context__)
        self.assertIsNone(raised.exception.__cause__)
        self.assertEqual(raised.exception.kind, "terminal")
        self.assertNotIn("private", "".join(traceback.format_exception(raised.exception)))

    def test_stop_before_start_does_not_launch_anything(self):
        stop = threading.Event()
        stop.set()
        with patch.object(media.subprocess, "Popen") as process, \
                patch.object(media, "find_ffmpeg") as find:
            self.assertEqual(list(self.source().chunks(stop)), [])
            process.assert_not_called()
            find.assert_not_called()

    def test_stalled_read_can_be_stopped_and_joins_all_workers(self):
        process = FakeProcess(stalled=True)
        self.launch(process)
        stop = threading.Event()
        errors = []

        def consume():
            try:
                list(self.source().chunks(stop))
            except Exception as exc:
                errors.append(exc)

        consumer = threading.Thread(target=consume)
        consumer.start()
        self.assertTrue(process.stdout.read_started.wait(1))
        stop.set()
        consumer.join(timeout=2)
        self.assertFalse(consumer.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(process.terminated.is_set())
        self.assert_released(process)

    def test_close_releases_a_generator_suspended_after_a_yield(self):
        process = FakeProcess(np.zeros(160, dtype="<f4").tobytes(), stalled=True)
        self.launch(process)
        chunks = self.source(chunk_seconds=0.01).chunks()
        self.assertEqual(len(next(chunks)), 160)
        chunks.close()
        self.assertTrue(process.terminated.is_set())
        self.assert_released(process)

    def test_stop_also_releases_process_while_consumer_is_suspended(self):
        process = FakeProcess(np.zeros(160, dtype="<f4").tobytes(), stalled=True)
        self.launch(process)
        stop = threading.Event()
        chunks = self.source(chunk_seconds=0.01).chunks(stop)
        next(chunks)
        stop.set()
        self.assertTrue(process.terminated.wait(1))
        # The consumer does not need to call next() for cancellation to work.
        chunks.close()
        self.assert_released(process)

    def test_start_and_midstream_stalls_have_finite_deadlines(self):
        for data in (b"", np.zeros(160, dtype="<f4").tobytes()):
            with self.subTest(has_audio=bool(data)):
                process = FakeProcess(data, stalled=True)
                with patch.object(media.subprocess, "Popen", return_value=process), \
                        patch.object(media, "_START_TIMEOUT_SECONDS", 0.1), \
                        patch.object(media, "_IDLE_TIMEOUT_SECONDS", 0.1):
                    with self.assertRaisesRegex(media.MediaError, "接收超时") as raised:
                        list(self.source(chunk_seconds=0.01).chunks())
                self.assertEqual(raised.exception.kind, "transient")
                self.assertTrue(process.terminated.is_set())
                self.assert_released(process)

    def test_unresponsive_process_is_killed_after_short_grace_period(self):
        process = FakeProcess(np.zeros(160, dtype="<f4").tobytes(), stalled=True,
                              ignore_terminate=True)
        self.launch(process)
        with patch.object(media, "_STOP_GRACE_SECONDS", 0.02):
            chunks = self.source(chunk_seconds=0.01).chunks()
            next(chunks)
            chunks.close()
        self.assertTrue(process.killed)
        self.assert_released(process)

    def test_backlog_is_skipped_and_reported_instead_of_aborting(self):
        raw = np.repeat(np.arange(10, dtype="<f4"), 160).tobytes()
        process = FakeProcess(stalled=True)
        process.stdout = GatedPipe(raw, process.release)
        self.launch(process)
        with patch.object(media, "_BUFFER_SECONDS", 0.03):
            source = self.source(chunk_seconds=0.01)
            chunks = source.chunks()
            try:
                self.assertTrue(np.array_equal(next(chunks), np.zeros(160, dtype=np.float32)))
                self.assertEqual((source.discontinuities, source.skipped_seconds), (0, 0.0))
                # ASR is busy with that first chunk while the bounded queue fills.
                process.stdout.resume.set()
                self.assertTrue(process.stdout.eof_reached.wait(2))
                received = []
                while len(received) < 3:
                    chunk = next(chunks)
                    received.append(int(chunk[0]))
                    if len(received) == 1:
                        self.assertGreater(chunk[0], 1)
                        self.assertEqual(source.discontinuities, 1)
                        self.assertAlmostEqual(source.skipped_seconds, (chunk[0] - 1) * 0.01)
                self.assertEqual(received, list(range(received[0], received[0] + 3)))
                self.assertEqual(source.discontinuities, 1)
                self.assertFalse(process.terminated.is_set())
            finally:
                chunks.close()
        self.assert_released(process)

    def test_skipped_audio_is_counted_before_the_first_chunk_after_the_gap(self):
        raw = np.repeat(np.arange(10, dtype="<f4"), 160).tobytes()
        process = FakeProcess(raw)
        self.launch(process)
        start = media._Decoder.start

        def start_and_wait_for_eof(decoder):
            start(decoder)
            self.assertTrue(process.stdout.eof_reached.wait(2))

        with patch.object(media, "_BUFFER_SECONDS", 0.03),                 patch.object(media._Decoder, "start", start_and_wait_for_eof):
            source = self.source(chunk_seconds=0.01)
            chunks = source.chunks()
            first = next(chunks)
            self.assertEqual(source.discontinuities, 1)
            self.assertAlmostEqual(source.skipped_seconds, first[0] * 0.01)
            rest = list(chunks)
        self.assertEqual([int(c[0]) for c in rest], list(range(int(first[0]) + 1, 10)))
        self.assertEqual(source.discontinuities, 1)
        self.assert_released(process)

    def test_user_stop_discards_pending_audio(self):
        raw = np.repeat(np.arange(10, dtype="<f4"), 160).tobytes()
        process = FakeProcess(stalled=True)
        process.stdout = GatedPipe(raw, process.release)
        self.launch(process)
        stop = threading.Event()
        with patch.object(media, "_BUFFER_SECONDS", 0.03):
            chunks = self.source(chunk_seconds=0.01).chunks(stop)
            try:
                next(chunks)
                process.stdout.resume.set()
                self.assertTrue(process.stdout.eof_reached.wait(2))
                stop.set()
                self.assertEqual(list(chunks), [])
            finally:
                chunks.close()
        self.assert_released(process)


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is not installed; mocked decoder tests still run")
class LocalFFmpegTests(unittest.TestCase):
    def test_local_stereo_wav_is_resampled_to_mono_float32(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "synthetic.wav"
            samples = np.sin(2 * np.pi * 440 * np.arange(4800) / 48000)
            stereo = np.stack([samples * 8000, samples * 16000], axis=1).astype("<i2")
            with wave.open(str(path), "wb") as output:
                output.setnchannels(2)
                output.setsampwidth(2)
                output.setframerate(48000)
                output.writeframes(stereo.tobytes())
            chunks = list(media.FFmpegAudioSource(str(path), chunk_seconds=0.02,
                                                  executable=shutil.which("ffmpeg")).chunks())
            decoded = np.concatenate(chunks)
            self.assertEqual(decoded.shape, (1600,))
            self.assertEqual(decoded.dtype, np.float32)
            self.assertTrue(np.isfinite(decoded).all())
            self.assertGreater(float(np.sqrt(np.mean(decoded ** 2))), 0.2)
            self.assertLess(float(np.max(np.abs(decoded))), 1)

    def test_local_video_without_audio_reports_missing_track(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "silent-video.mkv"
            executable = shutil.which("ffmpeg")
            subprocess.run([executable, "-hide_banner", "-loglevel", "error", "-nostdin",
                            "-f", "lavfi", "-i", "color=size=16x16:duration=0.1",
                            "-an", "-c:v", "ffv1", "-threads", "1", str(path)],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE, check=True, timeout=10,
                           **hidden_subprocess_options())
            with self.assertRaisesRegex(media.MediaError, "没有可用音轨"):
                list(media.FFmpegAudioSource(str(path), executable=executable).chunks())

    def test_http_flv_decodes_a_loopback_only_synthetic_stream(self):
        class QuietHandler(SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "synthetic.flv"
            executable = shutil.which("ffmpeg")
            subprocess.run([executable, "-hide_banner", "-loglevel", "error", "-nostdin",
                            "-f", "lavfi", "-i", "sine=frequency=440:duration=0.3",
                            "-c:a", "aac", "-threads", "1", "-f", "flv", str(path)],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE, check=True, timeout=10,
                           **hidden_subprocess_options())
            server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=folder))
            worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05), daemon=True)
            worker.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}/synthetic.flv"
                chunks = list(media.FFmpegAudioSource(url, headers={"User-Agent": "HDUSign offline test"},
                                                      executable=executable).chunks())
                decoded = np.concatenate(chunks)
                self.assertGreater(len(decoded), 4000)
                self.assertEqual(decoded.dtype, np.float32)
                self.assertTrue(np.isfinite(decoded).all())
                self.assertGreater(float(np.max(np.abs(decoded))), 0.05)
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=1)

    def test_slow_consumer_skips_backlog_and_keeps_the_real_stream_alive(self):
        class QuietHandler(SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "long.flv"
            executable = shutil.which("ffmpeg")
            subprocess.run([executable, "-hide_banner", "-loglevel", "error", "-nostdin",
                            "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
                            "-c:a", "aac", "-threads", "1", "-f", "flv", str(path)],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE, check=True, timeout=20,
                           **hidden_subprocess_options())
            server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=folder))
            worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05), daemon=True)
            worker.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}/long.flv"
                source = media.FFmpegAudioSource(url, chunk_seconds=0.25, executable=executable)
                received = []
                with patch.object(media, "_BUFFER_SECONDS", 0.5):
                    for chunk in source.chunks():
                        if not received:
                            # The recognizer stalls while the whole file arrives at once.
                            threading.Event().wait(1.5)
                        received.append(chunk)
                decoded = np.concatenate(received)
                self.assertGreaterEqual(source.discontinuities, 1)
                self.assertGreater(source.skipped_seconds, 1.0)
                self.assertLess(len(decoded) / media.SAMPLE_RATE + source.skipped_seconds, 6.6)
                self.assertGreater(len(decoded) / media.SAMPLE_RATE + source.skipped_seconds, 5.4)
                self.assertGreater(float(np.max(np.abs(received[-1]))), 0.05)
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=1)

    def test_disguised_hls_is_blocked_before_any_segment_request(self):
        requests = []

        class QuietHandler(SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def guess_type(self, path):
                return "application/vnd.apple.mpegurl"

            def do_GET(self):
                requests.append(self.path)
                super().do_GET()

        with tempfile.TemporaryDirectory() as folder:
            for name in ("disguised.flv", "stream"):
                (Path(folder) / name).write_text(
                    "#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXT-X-MEDIA-SEQUENCE:0\n"
                    "#EXTINF:1.0,\nnever-fetch.ts\n#EXT-X-ENDLIST\n", encoding="utf-8")
            server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=folder))
            worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05), daemon=True)
            worker.start()
            try:
                for name in ("disguised.flv", "stream"):
                    with self.subTest(path=name), self.assertRaisesRegex(
                            media.MediaError, "格式暂不支持后台音频") as raised:
                        list(media.FFmpegAudioSource(f"http://127.0.0.1:{server.server_port}/{name}",
                                                     executable=shutil.which("ffmpeg")).chunks())
                    self.assertEqual(raised.exception.kind, "terminal")
                self.assertEqual(requests, ["/disguised.flv", "/stream"])
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=1)


if __name__ == "__main__":
    unittest.main()
