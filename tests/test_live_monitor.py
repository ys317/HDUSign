"""Live recovery with virtual time and no school, microphone, push or sign-in."""
from contextlib import ExitStack, redirect_stdout
from io import StringIO
import os
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from hdusign import monitor
from hdusign.live import LiveEnded, LiveError, LiveLoginRequired, LiveTransientError
from hdusign.media import MediaError
from hdusign.rules import RuleMatcher, SignInWatcher


class VirtualClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class VirtualStop(threading.Event):
    def __init__(self, clock):
        super().__init__()
        self.clock = clock
        self.waits = []
        self.on_wait = None

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self.on_wait is not None:
            self.on_wait()
        if not self.is_set():
            self.clock.advance(timeout)
        return self.is_set()


class LiveMonitorTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {"live_audio": {"enabled": True}, "live_url":
                    "https://course.hdu.edu.cn/#/play-center?courseId=123&target=live"}
        self.clock = VirtualClock()
        self.stop = VirtualStop(self.clock)
        self.frame = np.zeros(4000, dtype=np.float32)

    def successful_frames(self, stop):
        yield self.frame
        stop.set()

    def setup_pipeline(self, stack, factory=None):
        stack.enter_context(patch.object(monitor.time, "monotonic", self.clock))
        client = MagicMock()
        client.__enter__.return_value = client
        stream = SimpleNamespace(url="https://example.invalid/live.flv", headers={})
        client.resolve.return_value = stream
        constructor = stack.enter_context(patch("hdusign.live.LiveClient", return_value=client))
        stack.enter_context(patch("hdusign.media.find_ffmpeg", return_value="ffmpeg.exe"))
        source = stack.enter_context(patch("hdusign.media.FFmpegAudioSource"))
        source.return_value.chunks.side_effect = factory or self.successful_frames
        source.return_value.discontinuities = 0
        source.return_value.skipped_seconds = 0.0
        alerter, signer, asr, watcher = MagicMock(), MagicMock(), MagicMock(), MagicMock()
        asr.accept.return_value = ([], "签到码一二三四")
        asr.revision = 1
        watcher.feed_partial.return_value = []
        stack.enter_context(patch.object(monitor, "make_alerter", return_value=alerter))
        make_signer = stack.enter_context(patch.object(monitor, "make_auto_signer", return_value=signer))
        make_asr = stack.enter_context(patch.object(monitor, "make_engine", return_value=asr))
        stack.enter_context(patch.object(monitor, "build_matchers", return_value=[]))
        stack.enter_context(patch.object(monitor, "make_watcher", return_value=watcher))
        output = StringIO()
        stack.enter_context(redirect_stdout(output))
        return SimpleNamespace(client=client, stream=stream, constructor=constructor, source=source,
                               alerter=alerter, signer=signer, asr=asr, watcher=watcher,
                               make_asr=make_asr, make_signer=make_signer, output=output)

    def test_direct_mode_does_not_start_a_loopback_capture(self):
        with patch.object(monitor, "cmd_run_live") as direct, patch.object(monitor, "LoopbackSource") as speaker:
            monitor.cmd_run(self.cfg, self.stop)
        direct.assert_called_once_with(self.cfg, self.stop)
        speaker.assert_not_called()

    def test_flapping_stream_has_bounded_backoff_without_finalizing_truncated_codes(self):
        closed = []

        def frames(stop):
            try:
                if len(closed) > 10:
                    self.fail("Recovery did not retain its bounded failure window")
                yield self.frame
            finally:
                closed.append(True)

        with ExitStack() as stack:
            f = self.setup_pipeline(stack, frames)
            with self.assertRaisesRegex(MediaError, "两分钟内未能恢复") as error:
                monitor.cmd_run_live(self.cfg, self.stop)
        self.assertFalse(error.exception.retryable)
        self.assertEqual(self.stop.waits, [2, 5, 10, 20, 30, 30, 23])
        self.assertEqual(self.clock.now, 120)
        self.assertEqual(f.client.resolve.call_count, 7)
        self.assertEqual(f.source.call_count, 7)
        self.assertEqual(len(closed), 7)
        self.assertEqual(f.asr.restart.call_count, 7)
        self.assertEqual(f.watcher.discard_partial.call_count, 7)
        f.asr.flush.assert_not_called()
        f.alerter.notify.assert_not_called()
        f.signer.submit.assert_not_called()
        f.signer.close.assert_called_once()
        f.client.__exit__.assert_called_once()
        f.constructor.assert_called_once_with(self.cfg["live_url"], self.cfg)

    def test_initial_api_outage_can_recover_before_loading_the_model(self):
        with ExitStack() as stack:
            f = self.setup_pipeline(stack)
            f.client.resolve.side_effect = [LiveTransientError("平台暂时不可用"), f.stream]
            self.stop.on_wait = lambda: f.make_asr.assert_not_called()
            monitor.cmd_run_live(self.cfg, self.stop)
        self.assertEqual(self.stop.waits, [2])
        self.assertEqual(f.client.resolve.call_count, 2)
        f.make_asr.assert_called_once()
        f.source.assert_called_once()

    def test_signin_page_prepares_before_live_lookup_and_reuses_the_session(self):
        with ExitStack() as stack:
            f = self.setup_pipeline(stack)
            def resolve():
                f.signer.prepare.assert_called_once()
                f.make_asr.assert_not_called()
                f.signer.submit.assert_not_called()
                return f.stream
            f.client.resolve.side_effect = resolve
            monitor.cmd_run_live(self.cfg, self.stop)
        f.signer.prepare.assert_called_once()
        f.signer.close.assert_called_once()

    def test_api_outage_during_reconnect_is_inside_the_recovery_window(self):
        connections = []

        def frames(stop):
            connections.append(1)
            yield self.frame
            if len(connections) == 2:
                stop.set()

        with ExitStack() as stack:
            f = self.setup_pipeline(stack, frames)
            f.client.resolve.side_effect = [f.stream, LiveTransientError("网络暂时不可用"), f.stream]
            monitor.cmd_run_live(self.cfg, self.stop)
        self.assertEqual(self.stop.waits, [2, 5])
        self.assertEqual(f.source.call_count, 2)
        self.assertEqual(f.asr.restart.call_count, 2)
        f.asr.flush.assert_not_called()

    def test_sustained_healthy_audio_renews_the_recovery_window(self):
        connections = []

        def frames(stop):
            connections.append(1)
            if len(connections) == 1:
                raise MediaError("网络中断")
            if len(connections) == 2:
                for _ in range(125):
                    self.clock.advance(0.25)
                    yield self.frame
                raise MediaError("另一次网络中断")
            yield self.frame
            stop.set()

        with ExitStack() as stack:
            f = self.setup_pipeline(stack, frames)
            monitor.cmd_run_live(self.cfg, self.stop)
        self.assertEqual(self.stop.waits, [2, 2])
        self.assertEqual(f.source.call_count, 3)
        f.make_asr.assert_called_once()
        f.asr.flush.assert_not_called()

    def test_fast_buffered_audio_does_not_masquerade_as_a_healthy_connection(self):
        connections = []

        def frames(stop):
            connections.append(1)
            if len(connections) > 10:
                self.fail("Fast buffered audio incorrectly reset the recovery budget")
            for _ in range(140):
                yield self.frame

        with ExitStack() as stack:
            f = self.setup_pipeline(stack, frames)
            with self.assertRaisesRegex(MediaError, "两分钟内未能恢复"):
                monitor.cmd_run_live(self.cfg, self.stop)
        self.assertEqual(len(connections), 7)
        self.assertEqual(self.clock.now, 120)
        f.asr.flush.assert_not_called()

    def test_configuration_and_format_errors_do_not_retry(self):
        with ExitStack() as stack:
            f = self.setup_pipeline(stack)
            f.source.return_value.chunks.side_effect = MediaError("直播格式不支持", kind="terminal")
            with self.assertRaisesRegex(MediaError, "格式不支持"):
                monitor.cmd_run_live(self.cfg, self.stop)
        f.source.assert_called_once()
        self.assertEqual(self.stop.waits, [])
        f.signer.close.assert_called_once()
        f.asr.flush.assert_not_called()

    def test_slow_trickle_during_recovery_cannot_exceed_its_deadline(self):
        closed = []
        connections = []

        def frames(stop):
            connections.append(1)
            try:
                if len(connections) == 1:
                    raise MediaError("连接中断")
                for _ in range(25):
                    self.clock.advance(10)
                    yield self.frame
            finally:
                closed.append(True)

        with ExitStack() as stack:
            f = self.setup_pipeline(stack, frames)

            def accept(chunk):
                self.assertLess(self.clock.now, 120, "Audio was accepted after the recovery deadline")
                return [], ""

            f.asr.accept.side_effect = accept
            with self.assertRaisesRegex(MediaError, "两分钟内未能恢复"):
                monitor.cmd_run_live(self.cfg, self.stop)
        self.assertEqual(f.asr.accept.call_count, 11)
        self.assertEqual(len(closed), 2)
        f.asr.flush.assert_not_called()

    def test_slow_resolution_cannot_start_a_decoder_after_the_recovery_deadline(self):
        with ExitStack() as stack:
            f = self.setup_pipeline(stack)
            calls = []

            def resolve():
                calls.append(1)
                if len(calls) == 1:
                    raise LiveTransientError("暂时不可用")
                self.clock.advance(130)
                return f.stream

            f.client.resolve.side_effect = resolve
            with self.assertRaisesRegex(MediaError, "两分钟内未能恢复"):
                monitor.cmd_run_live(self.cfg, self.stop)
        f.make_asr.assert_not_called()
        f.source.assert_not_called()
        self.assertEqual(len(calls), 2)

    def test_login_and_permission_errors_do_not_retry_or_start_capture(self):
        for error in (LiveLoginRequired("请登录直播"), LiveError("无权观看")):
            with self.subTest(error=type(error).__name__), ExitStack() as stack:
                f = self.setup_pipeline(stack)
                f.client.resolve.side_effect = error
                with self.assertRaises(type(error)):
                    monitor.cmd_run_live(self.cfg, self.stop)
                f.source.assert_not_called()
                f.make_asr.assert_not_called()
                f.signer.prepare.assert_called_once()
                f.signer.submit.assert_not_called()
                f.signer.close.assert_called_once()
        self.assertEqual(self.stop.waits, [])

    def test_expired_login_during_recovery_does_not_open_a_browser_or_retry(self):
        with ExitStack() as stack:
            f = self.setup_pipeline(stack, lambda stop: (chunk for chunk in [self.frame]))
            login = stack.enter_context(patch("hdusign.live.login_live"))
            f.client.resolve.side_effect = [f.stream, LiveLoginRequired("请重新登录直播")]
            with self.assertRaises(LiveLoginRequired):
                monitor.cmd_run_live(self.cfg, self.stop)
            login.assert_not_called()
        self.assertEqual(self.stop.waits, [2])
        f.source.assert_called_once()
        f.signer.close.assert_called_once()

    def test_known_course_end_returns_normally_without_switching_courses(self):
        with ExitStack() as stack:
            f = self.setup_pipeline(stack)
            f.client.resolve.side_effect = LiveEnded("本节已结束")
            monitor.cmd_run_live(self.cfg, self.stop)
        self.assertIn("本节直播已结束", f.output.getvalue())
        f.client.resolve.assert_called_once()
        f.source.assert_not_called()
        self.assertEqual(self.stop.waits, [])
        f.constructor.assert_called_once_with(self.cfg["live_url"], self.cfg)

    def test_rejected_media_token_is_resolved_once_before_requiring_login(self):
        with ExitStack() as stack:
            f = self.setup_pipeline(stack)
            f.source.return_value.chunks.side_effect = MediaError("直播音频授权失败", kind="auth")
            with self.assertRaisesRegex(MediaError, "授权无法刷新") as error:
                monitor.cmd_run_live(self.cfg, self.stop)
        self.assertFalse(error.exception.retryable)
        self.assertEqual(f.client.resolve.call_count, 2)
        self.assertEqual(f.source.call_count, 2)
        self.assertEqual(self.stop.waits, [2])
        f.asr.flush.assert_not_called()

    def test_api_outage_does_not_erase_a_previous_media_auth_failure(self):
        with ExitStack() as stack:
            f = self.setup_pipeline(stack)
            f.client.resolve.side_effect = [f.stream, LiveTransientError("API暂时不可用"), f.stream]
            f.source.return_value.chunks.side_effect = MediaError("媒体授权失败", kind="auth")
            with self.assertRaisesRegex(MediaError, "授权无法刷新"):
                monitor.cmd_run_live(self.cfg, self.stop)
        self.assertEqual(f.client.resolve.call_count, 3)
        self.assertEqual(f.source.call_count, 2)
        self.assertEqual(self.stop.waits, [2, 5])
        f.signer.close.assert_called_once()

    def test_stop_during_backoff_opens_no_further_connection(self):
        with ExitStack() as stack:
            f = self.setup_pipeline(stack, lambda stop: (chunk for chunk in [self.frame]))
            self.stop.on_wait = self.stop.set
            monitor.cmd_run_live(self.cfg, self.stop)
        f.client.resolve.assert_called_once()
        f.source.assert_called_once()
        f.signer.close.assert_called_once()
        f.asr.flush.assert_not_called()
        self.assertEqual(self.clock.now, 0)

    def test_stop_while_resolving_prevents_model_and_decoder_start(self):
        with ExitStack() as stack:
            f = self.setup_pipeline(stack)

            def resolved_after_stop():
                self.stop.set()
                return f.stream

            f.client.resolve.side_effect = resolved_after_stop
            monitor.cmd_run_live(self.cfg, self.stop)
        f.make_asr.assert_not_called()
        f.source.assert_not_called()
        f.client.__exit__.assert_called_once()

    def test_stopping_closes_the_audio_source_and_attendance_worker(self):
        closed = []

        def frames(stop):
            try:
                yield self.frame
                stop.set()
            finally:
                closed.append(True)

        with ExitStack() as stack:
            f = self.setup_pipeline(stack, frames)
            monitor.cmd_run_live(self.cfg, self.stop)
        self.assertEqual(closed, [True])
        f.client.resolve.assert_called_once()
        f.source.assert_called_once()
        f.signer.close.assert_called_once()
        f.asr.flush.assert_not_called()
        f.signer.submit.assert_not_called()

    def test_audio_fragments_on_opposite_sides_of_a_gap_cannot_form_a_signin_code(self):
        connections = []

        class Recognizer:
            revision = 0
            buffer = ""

            def accept(inner, chunk):
                inner.revision += 1
                inner.buffer += "签到码12" if chunk[0] == 1 else "34大家抓紧"
                return ([inner.buffer], "") if chunk[0] == 2 else ([], inner.buffer)

            def restart(inner):
                inner.buffer = ""
                inner.revision = 0

            def flush(inner):
                self.fail("A disconnected stream must never finalize unfinished audio")

        def frames(stop):
            connections.append(1)
            yield np.array([len(connections)], dtype=np.float32)
            if len(connections) == 2:
                stop.set()

        with ExitStack() as stack:
            f = self.setup_pipeline(stack, frames)
            watcher = SignInWatcher(log_file=os.devnull, early_code=True)
            watcher.on_code = lambda code, _: f.signer.submit(code)
            stack.enter_context(patch.object(monitor, "make_engine", return_value=Recognizer()))
            stack.enter_context(patch.object(monitor, "make_watcher", return_value=watcher))
            stack.enter_context(patch.object(monitor, "build_matchers", return_value=[RuleMatcher(["签到"], [])]))
            monitor.cmd_run_live(self.cfg, self.stop)
        f.signer.submit.assert_not_called()
        self.assertEqual(watcher.codes_found, [])
        self.assertEqual(len(connections), 2)

    def test_skipped_backlog_resets_recognition_without_reconnecting(self):
        class Recognizer:
            revision = 0
            buffer = ""
            restarts = 0

            def accept(inner, chunk):
                inner.revision += 1
                inner.buffer += {1: "签到码12", 2: "34大家抓紧", 3: "签到码5678好"}[int(chunk[0])]
                return ([inner.buffer], "") if chunk[0] != 1 else ([], inner.buffer)

            def restart(inner):
                inner.restarts += 1
                inner.buffer = ""
                inner.revision = 0

            def flush(inner):
                self.fail("A live stream must never be flushed")

        def frames(stop):
            source = f.source.return_value
            yield np.array([1], dtype=np.float32)
            # The decoder skipped a backlog before this chunk was yielded.
            source.discontinuities = 1
            source.skipped_seconds = 12.0
            yield np.array([2], dtype=np.float32)
            yield np.array([3], dtype=np.float32)
            stop.set()

        with ExitStack() as stack:
            f = self.setup_pipeline(stack, frames)
            recognizer = Recognizer()
            watcher = SignInWatcher(log_file=os.devnull, early_code=True)
            watcher.on_code = lambda code, _: f.signer.submit(code)
            stack.enter_context(patch.object(monitor, "make_engine", return_value=recognizer))
            stack.enter_context(patch.object(monitor, "make_watcher", return_value=watcher))
            stack.enter_context(patch.object(monitor, "build_matchers", return_value=[RuleMatcher(["签到"], [])]))
            monitor.cmd_run_live(self.cfg, self.stop)
        self.assertEqual(recognizer.restarts, 1)
        self.assertEqual([code for code, _ in watcher.codes_found], ["5678"])
        f.signer.submit.assert_called_once_with("5678")
        f.source.assert_called_once()
        f.client.resolve.assert_called_once()
        self.assertEqual(self.stop.waits, [])
        self.assertIn("已跳过约 12 秒", f.output.getvalue())

    def test_cancelled_start_opens_no_decoder_or_site_session(self):
        self.stop.set()
        with patch("hdusign.media.find_ffmpeg") as decoder, patch("hdusign.live.LiveClient") as client:
            monitor.cmd_run_live(self.cfg, self.stop)
        decoder.assert_not_called()
        client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
