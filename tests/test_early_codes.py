"""Early code confirmation must not promote unfinished numeric prefixes."""
from contextlib import redirect_stdout
from io import StringIO
import os
import unittest
from unittest.mock import Mock

import numpy as np

from hdusign.monitor import AudioClock, make_watcher, run_pipeline
from hdusign.rules import RuleMatcher, SignInWatcher


class EarlyCodeTests(unittest.TestCase):
    def setUp(self):
        self.clock = AudioClock()
        self.watcher = SignInWatcher(log_file=os.devnull, clock=self.clock, early_code=True)

    def partial(self, text, t, revision, cue=False):
        self.clock.seconds = t
        return self.watcher.feed_partial(text, revision=revision, audio_time=t, cue=cue)

    def test_long_sentence_can_commit_after_code_boundary_without_endpoint(self):
        self.assertEqual(self.partial("签到码一二三四", 1, 1, True), [])
        text = "签到码一二三四大家抓紧时间然后我们继续讲下一道题"
        self.assertEqual(self.partial(text, 1.5, 2, True), [("1234", text)])
        self.assertEqual(self.partial(text + "这一道题非常重要", 2, 3, True), [])

    def test_stale_preview_is_not_a_second_decoder_confirmation(self):
        text = "签到码一二三四大家抓紧"
        self.assertEqual(self.partial(text, 1, 8, True), [])
        self.assertEqual(self.partial(text, 3, 8, True), [])
        self.assertEqual(self.partial(text, 3.25, 9, True), [("1234", text)])

    def test_bare_tail_never_confirms_even_after_long_pause(self):
        self.assertEqual(self.partial("一八三六", 1, 1), [])
        self.assertEqual(self.partial("一八三六", 5, 8), [])
        self.assertEqual(self.partial("一八三六二", 5.5, 9), [])
        self.assertEqual(self.watcher.codes_found, [])

    def test_phone_decimal_and_units_cannot_be_closed_code_boundaries(self):
        for suffix in (" 2345678", ".56", "万元左右", "年以后", "号同学"):
            with self.subTest(suffix=suffix):
                self.setUp()
                self.partial("签到之后看1234" + suffix, 1, 1, True)
                self.assertEqual(self.partial("签到之后看1234" + suffix, 2, 2, True), [])

    def test_decimal_and_split_phone_suffixes_are_not_standalone_codes(self):
        for text in ("签到之后看3.1234大家", "签到之后看138 1234大家"):
            with self.subTest(text=text):
                self.setUp()
                self.partial(text, 1, 1, True)
                self.assertEqual(self.partial(text, 2, 2, True), [])

    def test_truncated_repeat_followed_by_speech_still_waits_for_final(self):
        self.partial("签到码七五三八", 1, 1, True)
        text = "签到码七五三八七五大家抓紧"
        self.assertEqual(self.partial(text, 2, 2, True), [])
        self.assertEqual(self.partial(text, 3, 3, True), [])

    def test_revision_resets_confirmation_instead_of_reusing_old_stability(self):
        self.partial("签到码六六六零大家输入", 1, 1, True)
        self.assertEqual(self.partial("签到码六六零九大家输入", 1.5, 2, True), [])
        self.assertEqual(self.partial("签到码六六零九大家输入", 2, 3, True),
                         [("6609", "签到码六六零九大家输入")])

    def test_complete_repeat_can_commit_but_truncated_repeat_cannot(self):
        self.partial("七五三八", 1, 1)
        self.assertEqual(self.partial("七五三八七五", 1.5, 2), [])
        self.assertEqual(self.partial("七五三八七五三八", 2, 3), [("7538", "七五三八七五三八")])

    def test_audio_gap_discards_candidate_stability_and_keeps_confirmed_dedup(self):
        self.assertEqual(self.watcher.feed("七五三八"), [("7538", "七五三八")])
        text = "签到码一二三四大家输入"
        self.assertEqual(self.partial(text, 1, 1, True), [])
        self.watcher.discard_partial()
        # A new connection can produce the same revision number. Evidence from
        # before the gap cannot count toward the confirmation window.
        self.assertEqual(self.partial(text, 3, 1, True), [])
        self.assertEqual(self.partial(text, 3.25, 2, True), [])
        self.assertEqual(self.partial(text, 3.5, 3, True), [("1234", text)])
        self.assertEqual(self.watcher.feed("七五三八"), [])
        self.assertEqual([code for code, _ in self.watcher.codes_found], ["7538", "1234"])

    def test_final_of_very_long_utterance_does_not_resubmit_early_code(self):
        self.partial("签到码一二三四大家", 1, 1, True)
        self.partial("签到码一二三四大家", 2, 2, True)
        self.clock.seconds = 100
        self.watcher.watch_until = 200
        self.assertEqual(self.watcher.feed("签到码一二三四大家继续听我讲"), [])
        self.clock.seconds = 101
        self.assertEqual(self.watcher.feed("一二三四"), [("1234", "一二三四")])

    def test_two_different_codes_in_one_utterance_are_kept(self):
        self.partial("签到码八三四八大家", 1, 1, True)
        self.partial("签到码八三四八大家", 2, 2, True)
        text = "签到码八三四八大家先签现在重新签到七八零二大家再签"
        self.assertEqual(self.partial(text, 3, 3, True), [])
        self.assertEqual(self.partial(text, 3.5, 4, True), [("7802", text)])
        self.assertEqual([c for c, _ in self.watcher.codes_found], ["8348", "7802"])

    def test_disabled_early_mode_still_prepares_and_uses_final_result(self):
        self.watcher.early_code = False
        self.partial("七五三八", 1, 1)
        self.assertEqual(self.partial("七五三八七五三八", 2, 2), [])
        self.assertEqual(self.watcher.feed("七五三八七五三八"), [("7538", "七五三八七五三八")])

    def test_pipeline_dispatches_early_code_before_final_sentence(self):
        class Recognizer:
            revision = 0

            def accept(inner, _):
                inner.revision += 1
                return [], ("签到码一二三四" if inner.revision == 1 else "签到码一二三四大家继续听课")

            def flush(inner):
                self.assertEqual(len(watcher.codes_found), 1)
                return ["签到码一二三四大家继续听课"]

        alerter = Mock()
        watcher = make_watcher({"code_watch": {"log_file": os.devnull}}, alerter, clock=self.clock)
        with redirect_stdout(StringIO()):
            run_pipeline(self.clock.advance([np.zeros(8000)] * 2), Recognizer(),
                         [RuleMatcher(["签到"], [])], alerter, watcher, show_partial=False, clock=self.clock)
        self.assertEqual([code for code, _ in watcher.codes_found], ["1234"])
        alerter.notify.assert_any_call("签到码一二三四大家继续听课", "code", "签到码: 1234")

    def test_empty_decoder_update_breaks_candidate_stability_in_pipeline(self):
        updates = iter(("一二三四大家", "", "一二三四大家"))

        class Recognizer:
            revision = 0

            def accept(inner, _):
                inner.revision += 1
                return [], next(updates)

            def flush(inner):
                self.assertEqual(self.watcher.codes_found, [])
                return []

        with redirect_stdout(StringIO()):
            run_pipeline(self.clock.advance([np.zeros(8000)] * 3), Recognizer(),
                         [], Mock(), self.watcher, show_partial=False, clock=self.clock)
        self.assertEqual(self.watcher.codes_found, [])


if __name__ == "__main__":
    unittest.main()
