"""Rule and code extraction checks, independent of user settings or models."""
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import numpy as np
import yaml

from hdusign.alert import Alerter
from hdusign.monitor import AudioClock, make_watcher, run_pipeline
from hdusign.rules import RuleMatcher
from hdusign.rules import SignInWatcher, extract_codes


class CodeExtractionTests(unittest.TestCase):
    """Spoken codes as they appear in real lecture transcripts."""

    def test_repeated_codes_merged_by_the_recognizer(self):
        cases = (
            ("密码六六零九六六零九", ["6609"]),      # said twice, merged into one run
            ("九九三八九九三", ["9938"]),            # second reading cut short
            ("七五三八七五三八", ["7538"]),
            ("四九零五四九零五四九零五", ["4905"]),
            ("八三四八", ["8348"]),
            ("签到码是1234", ["1234"]),
            ("签到码是幺2三4", ["1234"]),
            ("好哎今天还顺利一三二六三二六幺三二六", []),  # garbled, no repeat
            ("二零二六年二月通过三百八十二号法规", ["2026"]),  # unchanged legacy behaviour
            ("七八零二三这个电压就好", []),           # one trailing digit is not a repeat
            ("一八三六二", []),                       # five digits: ambiguous, never guess
            ("一8362", []),                         # Chinese digits also delimit ASCII runs
            ("8362一", []),
            ("八1234六", []),
            ("幺三九五七三八五七三八", []),           # phone-like run, not a repeated code
            ("电话号码13812345678不是签到码", []),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(extract_codes(text)[1], expected)

    def test_repeated_code_counts_as_standalone_but_long_numbers_do_not(self):
        with tempfile.TemporaryDirectory() as directory:
            watcher = SignInWatcher(standalone_code=True,
                                    log_file=str(Path(directory) / "codes.jsonl"))
            self.assertEqual(watcher.feed("七五三八七五三八", final=True)[0][0], "7538")
            self.assertEqual(watcher.feed("幺三九五七三八五七三八", final=True), [])
            self.assertEqual(watcher.feed("二零二六年二月通过三百八十二号法规", final=True), [])


class MatcherTests(unittest.TestCase):
    def test_attendance_phrases_do_not_match_time_ranges(self):
        template = Path(__file__).resolve().parents[1] / "config.example.yaml"
        rules = yaml.safe_load(template.read_text(encoding="utf-8"))["rules"]
        matcher = RuleMatcher(rules["strong"], rules["weak_groups"])
        cases = (
            ("那我们先点个到，然后开始做作业", "high"),
            ("现在开始点名了", "high"),
            ("二三三零", None),
            ("我们三点到五点再开始写作业", None),
            ("从两点到四点都是自习课", None),
            ("从源节点到目的节点转发数据包", None),
            ("这个节点到另一个节点的地址", None),
            ("大家先点到再开始上课", "high"),
            ("今天我们讲第二章第三节", None),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                result = matcher.match(text)
                self.assertEqual(result[0] if result else None, expected)

    def test_standalone_codes_and_attendance_window(self):
        with tempfile.TemporaryDirectory() as directory:
            watcher = SignInWatcher(standalone_code=True,
                                    log_file=str(Path(directory) / "codes.jsonl"))
            self.assertEqual(watcher.feed("二三三零", final=True)[0][0], "2330")
            self.assertEqual(watcher.feed("今天讲第二章第三节，然后做二十分钟作业", final=True), [])
            watcher.trigger("现在开始签到", "签到提示")
            self.assertEqual(watcher.feed("签到码四五六七", final=True)[0][0], "4567")
            self.assertEqual(watcher.feed("五六七八", final=True)[0][0], "5678")


class ReplayClockTests(unittest.TestCase):
    """Windows and de-duplication follow the injected clock, not decoding speed."""

    def test_watch_window_and_code_dedup_use_injected_clock(self):
        clock = AudioClock()
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(StringIO()):
            watcher = SignInWatcher(window_seconds=60, code_dedup=60, standalone_code=False,
                                    log_file=str(Path(directory) / "codes.jsonl"), clock=clock)
            self.assertEqual(watcher.feed("签到码是四五六七", final=True), [])
            watcher.trigger("现在开始签到", "签到提示")
            self.assertTrue(watcher.active)
            self.assertEqual(watcher.feed("签到码是四五六七", final=True)[0][0], "4567")
            clock.seconds = 30
            self.assertEqual(watcher.feed("四五六七", final=True), [], "same code within dedup")
            clock.seconds = 59
            self.assertTrue(watcher.active)
            clock.seconds = 61
            self.assertFalse(watcher.active)
            self.assertEqual(watcher.feed("四五六七", final=True), [], "window closed")
            watcher.trigger("再签一次", "签到提示")
            clock.seconds = 100  # inside the new window, past the code dedup interval
            self.assertEqual(watcher.feed("四五六七", final=True)[0][0], "4567")

    def test_alert_dedup_uses_injected_clock(self):
        clock = AudioClock()
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(StringIO()):
            alerter = Alerter(str(Path(directory) / "alerts.jsonl"), dedup_seconds=90, clock=clock)
            self.assertTrue(alerter.notify("现在开始签到", "high", "强关键词:签到"))
            clock.seconds = 89
            self.assertFalse(alerter.notify("现在开始签到", "high", "强关键词:签到"))
            clock.seconds = 91
            self.assertTrue(alerter.notify("现在开始签到", "high", "强关键词:签到"))

    def test_audio_clock_advances_to_the_end_of_each_chunk(self):
        clock = AudioClock()
        chunks = [np.zeros(4000, dtype=np.float32), np.zeros(16000, dtype=np.float32)]
        positions = []
        for chunk in clock.advance(chunks):
            positions.append(clock())
            self.assertEqual(len(chunk), len(chunks[len(positions) - 1]))
        self.assertEqual(positions, [0.25, 1.25])
        self.assertEqual(clock.stamp(), "00:00:01")


class StableCodeTests(unittest.TestCase):
    """Exercise the live dispatch path with recognition revisions from lectures."""

    def replay(self, results, tail=()):
        clock = AudioClock()
        signer = Mock()
        asr = Mock()
        asr.accept.side_effect = results
        asr.flush.return_value = list(tail)
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(StringIO()):
            alerter = Alerter(str(Path(directory) / "alerts.jsonl"), clock=clock)
            watcher = make_watcher(
                {"code_watch": {"log_file": str(Path(directory) / "codes.jsonl")}},
                alerter, signer, clock=clock)
            chunks = [np.zeros(4000, dtype=np.float32) for _ in results]
            run_pipeline(clock.advance(chunks), asr, [RuleMatcher(["签到"], [])],
                         alerter, watcher, show_partial=False, clock=clock)
            return [call.args[0] for call in signer.submit.call_args_list], watcher.codes_found

    def test_partial_four_digits_never_submit_a_five_digit_or_phone_prefix(self):
        for prefix, final in (("一八三六", "一八三六二"),
                              ("1381", "13812345678")):
            with self.subTest(final=final):
                submitted, found = self.replay([
                    (["现在开始签到"], ""), ([], prefix), ([final], ""),
                ])
                self.assertEqual(submitted, [])
                self.assertEqual(found, [])

    def test_revision_commits_only_the_final_code(self):
        submitted, found = self.replay([
            (["现在开始签到"], ""), ([], "六六六零"), ([], "六六零九"),
            (["密码六六零九六六零九"], ""), (["六六零九"], ""),
        ])
        self.assertEqual(submitted, ["6609"])
        self.assertEqual(found, [("6609", "密码六六零九六六零九")])

    def test_standalone_repeated_code_is_dispatched_once_after_finalization(self):
        submitted, _ = self.replay([
            ([], "七五三八"), ([], "七五三八七五"),
            (["七五三八七五三八"], ""), (["七五三八"], ""),
        ])
        self.assertEqual(submitted, ["7538"])

    def test_end_of_file_uses_completed_result_instead_of_last_partial(self):
        submitted, _ = self.replay([
            (["现在开始签到"], ""), ([], "一八三六"),
        ], tail=["一八三六二"])
        self.assertEqual(submitted, [])
        submitted, _ = self.replay([([], "四五六七")], tail=["四五六七"])
        self.assertEqual(submitted, ["4567"])

    def test_watcher_rejects_partial_input_without_side_effects(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(StringIO()):
            log = Path(directory) / "codes.jsonl"
            watcher = SignInWatcher(log_file=str(log))
            watcher.trigger("现在开始签到", "签到提示")
            self.assertEqual(watcher.feed("一八三六", final=False), [])
            self.assertEqual(watcher.codes_found, [])
            self.assertFalse(watcher.recent)
            self.assertFalse(log.exists())
            self.assertEqual(watcher.feed("一八三六", final=True), [("1836", "一八三六")])


if __name__ == "__main__":
    unittest.main()
