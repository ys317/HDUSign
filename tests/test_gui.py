r"""Offline desktop regressions. Uses a withdrawn window and temporary settings.

Run with: .venv\Scripts\python.exe -m unittest discover -s tests -p test_gui.py -v
No microphone, browser login, signing or webhook requests are started.
"""
from __future__ import annotations

import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from hdusign import gui as ui


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        (ui.APP_ROOT / "build").mkdir(exist_ok=True)
        cls.temp = tempfile.TemporaryDirectory(dir=ui.APP_ROOT / "build")
        cls.root = Path(cls.temp.name)
        cls.original_paths = ui.CONFIG, ui.SECRETS
        ui.CONFIG = cls.root / "config.yaml"
        ui.SECRETS = cls.root / "secrets_local.json"
        cls.base_cfg = {
            "live_url": "https://example.com/class",
            "live_audio": {"enabled": False},
            "location": {"lat": 29.2, "lng": 119.4},
            "auto_sign": {"enabled": False, "timeout_seconds": 180},
            "rules": {"strong": ["签到", "点名"], "semantic": {"enabled": True}},
            "alert": {"webhook": {"url": "", "levels": ["high", "code"]}},
            "device": "test-device",
        }
        ui.CONFIG.write_text(yaml.safe_dump(cls.base_cfg), encoding="utf-8")
        ui.SECRETS.write_text(json.dumps({
            "skl_username": "demo-student", "skl_password": "test-password"}), encoding="utf-8")
        cls.app = ui.App()
        cls.app.withdraw()
        cls.app.update()

    @classmethod
    def tearDownClass(cls):
        for job in cls.app.tk.splitlist(cls.app.tk.call("after", "info")):
            # Cancel timers without deleting Tcl commands owned by child widgets.
            cls.app.tk.call("after", "cancel", job)
        cls.app.destroy()
        ui.ctk.AppearanceModeTracker.update_loop_running = False
        ui.ctk.ScalingTracker.update_loop_running = False
        ui.CONFIG, ui.SECRETS = cls.original_paths
        cls.temp.cleanup()

    def setUp(self):
        self.app._loading = True
        self.app._needs_live_login = False
        self.app.cfg = copy.deepcopy(self.base_cfg)
        self.app.txt_rules.delete("1.0", "end")
        self.app._load_fields()
        self.app._saved_values = self.app._field_values()
        self.app._loading = False
        self.app.txt_rules.edit_modified(False)
        self.app.stop_event.clear()
        self.app._set_code(None)
        self.app.clear_log()
        for q in (self.app.log_q, self.app.task_q):
            while not q.empty():
                q.get_nowait()

    def pump_until(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.update()
            if predicate():
                return
            time.sleep(0.01)
        self.fail("GUI task did not reach the expected state")

    def test_saves_fields_without_discarding_other_settings(self):
        self.app.v_auto.set(True)
        self.app.v_sem.set(False)
        self.app.v_lat.set(" 30.25 ")
        self.app.v_user.set(" demo-updated ")
        self.assertTrue(self.app.save_cfg())
        saved = yaml.safe_load(ui.CONFIG.read_text(encoding="utf-8"))
        self.assertEqual(saved["location"]["lat"], 30.25)
        self.assertTrue(saved["auto_sign"]["enabled"])
        self.assertFalse(saved["rules"]["semantic"]["enabled"])
        self.assertEqual(saved["device"], "test-device")
        self.assertEqual(saved["alert"]["webhook"]["levels"], ["high", "code"])
        self.assertEqual(saved["auto_sign"]["timeout_seconds"], 180)
        self.assertEqual(json.loads(ui.SECRETS.read_text())["skl_username"], "demo-updated")

    def test_invalid_coordinates_do_not_write_files(self):
        original = ui.CONFIG.read_bytes(), ui.SECRETS.read_bytes()
        for value in ("", " ", "not a number", "nan", "inf", "91", "-91"):
            with self.subTest(value=value):
                self.app.v_lat.set(value)
                self.assertFalse(self.app.save_cfg())
                self.assertEqual(original, (ui.CONFIG.read_bytes(), ui.SECRETS.read_bytes()))
                self.assertEqual(self.app._active_tab, "extras")
                self.assertTrue(self.app._entries["lat"].invalid)
        self.app.v_lat.set("30")
        self.app.v_lng.set("181")
        self.assertFalse(self.app.save_cfg())

    def test_location_defaults_and_saved_values_reach_the_browser(self):
        from hdusign import browser

        self.app.cfg.pop("location")
        self.app._load_fields()
        self.assertEqual(self.app.v_lat.get(), "30.314732")
        self.assertEqual(self.app.v_lng.get(), "120.343727")
        self.assertTrue(self.app.save_cfg())
        with patch.object(browser, "ROOT", self.root):
            self.assertEqual(browser._location(), (30.314732, 120.343727))
        self.app.v_lat.set("31.123456")
        self.app.v_lng.set("121.654321")
        self.assertTrue(self.app.save_cfg())
        with patch.object(browser, "ROOT", self.root):
            self.assertEqual(browser._location(), (31.123456, 121.654321))

    def test_monitor_can_start_with_empty_live_url(self):
        self.app.v_url.set("")
        with patch.object(self.app, "_worker", return_value=True) as worker:
            self.app.start_monitor()
        self.assertEqual(worker.call_args.kwargs["kind"], "monitor")
        self.assertEqual(worker.call_args.args[1]["live_url"], "")

    def test_background_audio_requires_a_url_and_remembers_the_mode(self):
        self.app.v_live.set(True)
        self.app.v_url.set("")
        with patch.object(self.app, "_worker") as worker:
            self.app.start_monitor()
            worker.assert_not_called()
        self.assertEqual(self.app._active_tab, "basic")
        self.assertIn("请选择直播课程", self.app._save_state.cget("text"))
        self.app.v_url.set("https://course.hdu.edu.cn/#/play-center?courseId=123&target=live")
        with patch.object(self.app, "_worker", return_value=True) as worker:
            self.app.start_monitor()
        self.assertTrue(worker.call_args.args[1]["live_audio"]["enabled"])
        self.assertEqual(self.app._active_audio_source, "live")
        self.assertTrue(yaml.safe_load(ui.CONFIG.read_text(encoding="utf-8"))["live_audio"]["enabled"])

    def test_live_login_is_available_without_a_url_in_both_audio_modes(self):
        for direct in (False, True):
            self.app.v_live.set(direct)
            self.app.v_url.set("")
            with patch.object(self.app, "_worker", return_value=True) as worker:
                self.app.do_live_login()
            self.assertEqual(worker.call_args.args[1], "https://course.hdu.edu.cn/#/home")
            self.assertEqual(self.app.b_live_login.cget("text"), "登录并读取课程")
            self.assertEqual(self.app.b_url.cget("text"), "打开 ↗")

    def test_background_audio_login_is_disabled_while_monitoring(self):
        release = threading.Event()
        self.addCleanup(release.set)
        self.app.v_live.set(True)
        self.app._worker(lambda: release.wait(2), kind="monitor")
        self.assertEqual(self.app.b_live_login.cget("state"), "disabled")
        self.assertEqual(self.app.b_live_account.cget("state"), "disabled")
        with patch.object(self.app, "save_cfg") as save:
            self.app.do_live_login()
            save.assert_not_called()
        release.set()
        self.pump_until(lambda: self.app._task_kind is None)
        self.assertEqual(self.app.b_live_login.cget("state"), "normal")
        self.assertEqual(self.app.b_live_account.cget("state"), "normal")

    def test_home_login_passes_entered_credentials_and_loads_courses(self):
        self.app.v_user.set(" demo-home ")
        self.app.v_pwd.set("home-password")
        self.app.v_url.set("")
        with patch("hdusign.live.login_live", return_value=0) as login, \
                patch.object(self.app, "refresh_live_courses") as refresh:
            self.app.b_live_login.invoke()
            self.assertEqual(self.app._entries["user"].cget("state"), "disabled")
            self.assertEqual(self.app.b_monitor.cget("state"), "disabled")
            self.pump_until(lambda: self.app._task_kind is None)
        login.assert_called_once_with(
            "https://course.hdu.edu.cn/#/home", self.app.stop_event,
            credentials={"skl_username": "demo-home", "skl_password": "home-password"})
        refresh.assert_called_once()
        self.assertFalse(self.app._needs_live_login)

    def test_home_login_requires_both_fields_before_saving_or_opening_browser(self):
        for user, password, field in ((" ", "password", "user"), ("student", "", "pwd")):
            self.app.v_user.set(user)
            self.app.v_pwd.set(password)
            with patch.object(self.app, "save_cfg") as save, patch.object(self.app, "_worker") as worker:
                self.app.do_live_login()
            save.assert_not_called()
            worker.assert_not_called()
            self.assertEqual(self.app._active_tab, "extras")
            self.assertTrue(self.app._entries[field].invalid)

    def test_failed_login_cannot_refresh_or_monitor_with_previous_session(self):
        self.app.v_live.set(True)
        with patch("hdusign.live.login_live", return_value=1):
            self.app.do_live_login()
            self.pump_until(lambda: self.app._task_kind is None)
        self.assertTrue(self.app._needs_live_login)
        self.assertFalse(self.app._live_course_lookup)
        self.assertIn("登录未完成", self.app._live_course_hint.cget("text"))
        with patch.object(self.app, "_worker") as worker:
            self.app.refresh_live_courses()
            self.assertFalse(self.app.start_monitor())
        worker.assert_not_called()

    def test_single_home_action_follows_selected_course_start_time(self):
        from hdusign.live import LiveCourse
        now = time.time()
        current = LiveCourse("100", "当前课", now - 60, now + 1800)
        future = LiveCourse("101", "稍后课", now + 3600, now + 5400)
        self.app._set_live_course_choices([current, future])
        labels = list(self.app._live_course_lookup)
        self.app._select_live_course(labels[1])
        self.assertEqual(self.app.b_monitor.cget("text"), "预约直播")
        self.assertEqual(self.app.b_monitor.cget("state"), "normal")
        with patch.object(self.app, "_schedule_monitor") as schedule, \
                patch.object(self.app, "start_monitor") as start:
            self.app.b_monitor.invoke()
            schedule.assert_called_once_with(future)
            start.assert_not_called()
            self.app._select_live_course(labels[0])
            self.assertEqual(self.app.b_monitor.cget("text"), "开始监控")
            self.app.b_monitor.invoke()
            start.assert_called_once()
            schedule.assert_called_once()

    def test_action_changes_at_start_time_without_reselecting_course(self):
        from hdusign.live import LiveCourse
        now = time.time()
        course = LiveCourse("101", "即将开课", now + 60, now + 3600)
        self.app._set_live_course_choices([course])
        self.app._select_live_course(next(iter(self.app._live_course_lookup)))
        self.assertEqual(self.app.b_monitor.cget("text"), "预约直播")
        with patch.object(ui.time, "time", return_value=course.start), \
                patch.object(self.app, "after", return_value=self.app._tick_job):
            self.app._tick()
        self.assertEqual(self.app.b_monitor.cget("text"), "开始监控")
        self.assertEqual(self.app.b_monitor.cget("state"), "normal")

    def test_activity_details_are_collapsed_and_keep_logs_when_toggled(self):
        self.assertFalse(self.app._log_details.winfo_manager())
        self.app._append_log("[i] hidden activity")
        self.app.b_details.invoke()
        self.assertEqual(self.app._log_details.winfo_manager(), "pack")
        self.assertIn("hidden activity", self.app.log.get("1.0", "end"))
        self.app.b_details.invoke()
        self.assertFalse(self.app._log_details.winfo_manager())
        self.assertIn("hidden activity", self.app.log.get("1.0", "end"))

    def test_account_edit_clears_course_and_cancels_previous_reservation(self):
        from hdusign.live import LiveCourse
        course = LiveCourse("101", "稍后课", time.time() + 3600, time.time() + 5400)
        self.app._set_live_course_choices([course])
        self.app._select_live_course(next(iter(self.app._live_course_lookup)))
        self.app._scheduled_course = course
        self.app.v_user.set("different-student")
        self.assertIsNone(self.app._scheduled_course)
        self.assertFalse(self.app._live_course_lookup)
        self.assertEqual(self.app.v_url.get(), "")
        self.assertTrue(self.app._needs_live_login)
        self.assertNotIn("scheduled_course", yaml.safe_load(ui.CONFIG.read_text(encoding="utf-8")))

    def test_startup_does_not_restore_a_different_accounts_live_session(self):
        session = self.root / "live_session.json"
        session.write_text(json.dumps({"username": "another-student"}), encoding="utf-8")
        try:
            with patch.object(self.app, "refresh_live_courses") as refresh:
                self.app._load_saved_live_courses()
            refresh.assert_not_called()
            self.assertTrue(self.app._needs_live_login)
        finally:
            session.unlink()

    def test_live_account_switch_requests_a_separate_manual_login(self):
        url = "https://course.hdu.edu.cn/#/play-center?courseId=123&target=live"
        self.app.v_live.set(True)
        self.app.v_url.set(url)
        with patch("hdusign.live.login_live", return_value=0) as login, \
                patch.object(self.app, "refresh_live_courses") as refresh:
            self.app.b_live_account.invoke()
            self.pump_until(lambda: self.app._task_kind is None)
        login.assert_called_once_with(url, self.app.stop_event, switch_account=True)
        refresh.assert_called_once()

    def test_course_refresh_uses_unsaved_url_and_applies_selection_before_monitor(self):
        from hdusign.live import LiveCourse, live_course_url
        course = LiveCourse("123", "网络安全", 1790038500, 1790041200, tecl_id="456")
        url = "https://course.hdu.edu.cn/#/home"
        self.app.v_url.set(url)
        with patch("hdusign.live.list_live_courses", return_value=[course]) as read:
            self.app.refresh_live_courses()
            self.pump_until(lambda: self.app._task_kind is None)
        self.assertEqual(read.call_args.args[0]["live_url"], url)
        self.assertEqual(self.app.v_url.get(), url)
        self.app._select_live_course(next(iter(self.app._live_course_lookup)))
        with patch.object(self.app, "_worker", return_value=True) as worker:
            self.app.start_monitor()
        cfg = worker.call_args.args[1]
        self.assertEqual(cfg["live_url"], live_course_url(course))
        self.assertTrue(cfg["live_audio"]["enabled"])

    def test_failed_refresh_clears_stale_courses_and_preserves_manual_url(self):
        from hdusign.live import LiveCourse, LiveLoginRequired
        self.app._set_live_course_choices([LiveCourse("123", "课堂", 1, 2)])
        before = self.app.v_url.get()
        with patch("hdusign.live.list_live_courses", side_effect=LiveLoginRequired("请登录直播")):
            self.app.refresh_live_courses()
            self.pump_until(lambda: self.app._task_kind is None)
        self.assertFalse(self.app._live_course_lookup)
        self.assertFalse(self.app._course_cards)
        self.assertEqual(self.app.v_url.get(), before)
        self.assertIn("请登录直播", self.app._live_course_hint.cget("text"))

    def test_saved_login_triggers_auto_refresh_without_starting_a_login(self):
        session = self.root / "live_session.json"
        with patch.object(self.app, "refresh_live_courses") as refresh:
            self.app._load_saved_live_courses()
            refresh.assert_not_called()
            session.write_text("{}", encoding="utf-8")
            try:
                self.app._load_saved_live_courses()
                refresh.assert_called_once()
            finally:
                session.unlink()

    def test_live_reconnect_clears_partial_preview_and_keeps_confirmed_code(self):
        release = threading.Event()
        self.addCleanup(release.set)
        self.app._active_audio_source = "live"
        self.app._worker(lambda: release.wait(2), kind="monitor")
        self.app._append_log("[i] ASR 就绪 · 正在接收直播音频")
        self.app._append_log("…识别中: 签到码一二")
        self.app._set_code("7538")
        self.app._append_log("[i] 直播状态: 正在重连")
        self.assertEqual(self.app._status.cget("text"), "正在重连")
        self.assertNotIn("签到码一二", self.app._transcript.cget("text"))
        self.assertEqual(self.app._code, "7538")
        self.assertFalse(self.app._has_transcript)
        self.app._append_log("[i] ASR 就绪 · 正在接收直播音频")
        self.assertEqual(self.app._status.cget("text"), "监控中")
        self.app.stop_monitor()
        self.app._append_log("[i] 直播状态: 正在重连")
        self.app._append_log("[i] ASR 就绪 · 正在接收直播音频")
        self.assertEqual(self.app._status.cget("text"), "正在停止")
        release.set()
        self.pump_until(lambda: self.app._task_kind is None)

    def test_location_runs_in_background_and_is_applied_on_main_thread(self):
        original = ui.CONFIG.read_bytes(), ui.SECRETS.read_bytes()
        previous = self.app.v_lat.get(), self.app.v_lng.get()
        release = threading.Event()
        self.addCleanup(release.set)
        caller_threads = []
        main_thread = threading.get_ident()
        original_set = self.app.v_lat.set

        def record_set(value):
            caller_threads.append(threading.get_ident())
            original_set(value)

        def locate():
            release.wait(2)
            return ui.Location(30.321456, 120.345678, 48)

        with patch.object(ui, "get_current_location", side_effect=locate), \
                patch.object(self.app.v_lat, "set", side_effect=record_set):
            self.app.locate()
            self.assertEqual(self.app._task_kind, "location")
            self.assertEqual(self.app.b_locate.cget("state"), "disabled")
            self.assertEqual(self.app.b_monitor.cget("state"), "disabled")
            self.assertEqual((self.app.v_lat.get(), self.app.v_lng.get()), previous)
            release.set()
            self.pump_until(lambda: self.app._task_kind is None)
        self.assertEqual(caller_threads, [main_thread])
        self.assertEqual((self.app.v_lat.get(), self.app.v_lng.get()), ("30.321456", "120.345678"))
        self.assertIn("48 米", self.app._location_hint.cget("text"))
        self.assertIn("未保存", self.app._save_state.cget("text"))
        self.assertEqual(original, (ui.CONFIG.read_bytes(), ui.SECRETS.read_bytes()))
        self.assertEqual(self.app.b_locate.cget("state"), "normal")
        self.assertEqual(self.app.b_monitor.cget("state"), "normal")
        self.assertTrue(self.app.save_cfg())
        self.assertEqual(yaml.safe_load(ui.CONFIG.read_text(encoding="utf-8"))["location"],
                         {"lat": 30.321456, "lng": 120.345678})

    def test_failed_location_preserves_coordinates_and_recovers_controls(self):
        before = self.app.v_lat.get(), self.app.v_lng.get()
        with patch.object(ui, "get_current_location", side_effect=ui.LocationError("Windows 定位已关闭")):
            self.app.locate()
            self.pump_until(lambda: self.app._task_kind is None)
        self.assertEqual((self.app.v_lat.get(), self.app.v_lng.get()), before)
        self.assertIn("定位已关闭", self.app._location_hint.cget("text"))
        self.assertEqual(self.app.b_locate.cget("state"), "normal")
        self.assertEqual(self.app.b_monitor.cget("state"), "normal")

    def test_location_does_not_overwrite_edits_made_while_waiting(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def locate():
            release.wait(2)
            return ui.Location(30.321456, 120.345678, 48)

        with patch.object(ui, "get_current_location", side_effect=locate):
            self.app.locate()
            self.app.v_lat.set("32.123456")
            self.app.v_lng.set("121.234567")
            release.set()
            self.pump_until(lambda: self.app._task_kind is None)
        self.assertEqual((self.app.v_lat.get(), self.app.v_lng.get()), ("32.123456", "121.234567"))
        self.assertIn("保留当前填写", self.app._location_hint.cget("text"))

    def test_actions_do_not_start_after_validation_failure(self):
        self.app.v_lng.set("invalid")
        self.app.v_hook.set("https://example.com/webhook")
        with patch.object(self.app, "_worker") as worker:
            self.app.start_monitor()
            self.app.do_login()
            self.app.test_webhook()
            worker.assert_not_called()

    def test_save_failure_has_inline_feedback(self):
        before = copy.deepcopy(self.app.cfg)
        self.app.v_url.set("https://example.com/new")
        with patch.object(Path, "write_text", side_effect=PermissionError("denied")):
            self.assertFalse(self.app.save_cfg())
        self.assertEqual(self.app.cfg, before)
        self.assertIn("保存失败", self.app._save_state.cget("text"))

    def test_switch_tracks_programmatic_changes_and_rapid_clicks(self):
        var = ui.ctk.BooleanVar(value=False)
        switch = ui.Switch(self.app, var)
        try:
            var.set(True)
            self.pump_until(lambda: switch._position == switch._x(True))
            self.assertEqual(switch.cget("fg_color"), ui.design.SW_ON)
            for _ in range(7):
                switch.toggle()
            self.pump_until(lambda: switch._position == switch._x(False))
            self.assertFalse(var.get())
            self.assertEqual(switch.cget("fg_color"), ui.design.SW_OFF)
        finally:
            switch.destroy()

    def test_live_partial_does_not_flood_log(self):
        for i in range(30):
            self.app._append_log(f"…识别中: 测试课堂内容 {i}")
        self.assertEqual(self.app.log.get("1.0", "end-1c"), "")
        self.assertIn("29", self.app._transcript.cget("text"))
        self.app._append_log("[ASR 12:00:00] 现在开始签到")
        self.assertEqual(self.app._transcript.cget("text"), "现在开始签到")
        self.app._append_log("*** 签到提醒 [code] 12:00:00: 签到码: 2330")
        self.assertEqual("".join(box.cget("text") for box in self.app._digit_boxes), "2330")

    def test_log_retention_and_follow_toggle(self):
        self.app.v_follow.set(False)
        with patch.object(ui, "MAX_LOG_LINES", 20), patch.object(self.app.log, "see") as see:
            for i in range(70):
                self.app._append_log(f"[i] event-{i:03d}")
            see.assert_not_called()
        lines = self.app.log.get("1.0", "end-1c").splitlines()
        self.assertEqual(len(lines), 20)
        self.assertIn("event-069", lines[-1])
        self.assertIn("event-050", lines[0])
        self.app.v_follow.set(True)

    def test_copy_is_available_only_after_a_code_is_detected(self):
        with patch.object(self.app, "clipboard_clear") as clear, \
                patch.object(self.app, "clipboard_append") as append:
            self.assertEqual(self.app.b_copy.cget("state"), "disabled")
            self.assertFalse(self.app.copy_code())
            clear.assert_not_called()
            self.app._append_log("[i] 签到码: 0234")
            self.assertEqual(self.app.b_copy.cget("state"), "normal")
            self.assertTrue(self.app.copy_code())
            clear.assert_called_once()
            append.assert_called_once_with("0234")
            self.assertEqual(self.app.b_copy.cget("text"), "已复制")
            self.app._append_log("[i] 签到码: 5678")
            self.assertEqual(self.app.b_copy.cget("text"), "复制")
            self.assertTrue(self.app.copy_code())
            self.assertEqual(append.call_args.args, ("5678",))
            self.pump_until(lambda: self.app.b_copy.cget("text") == "复制")
            self.assertEqual(self.app._code, "5678")

    def test_clearing_activity_keeps_detected_code_and_timer(self):
        self.app._append_log("[i] 签到码: 1234")
        self.assertEqual(len(self.app._activity_records), 1)
        self.app._metrics["time"].configure(text="00:12:34")
        self.app.clear_log()
        self.assertEqual("".join(box.cget("text") for box in self.app._digit_boxes), "1234")
        self.assertEqual(self.app.b_copy.cget("state"), "normal")
        self.assertEqual(self.app._metrics["time"].cget("text"), "00:12:34")
        self.assertEqual(self.app._activity_records, [])

    def test_activity_toggle_keeps_recent_events_and_ignores_transcripts(self):
        self.app._set_log_details(False)
        self.app._append_log("[ASR 12:00:00] 现在开始签到")
        self.assertEqual(self.app._activity_records, [])
        for i in range(25):
            self.app._append_log(f"[i] 签到码: {1000 + i}")
        self.assertEqual(len(self.app._activity_records), 20)
        self.assertIn("1005", self.app._activity_records[0]["text"])
        self.assertIn("1024", self.app._activity_records[-1]["text"])
        self.assertFalse(self.app._log_details.winfo_manager())
        self.app.b_details.invoke()
        self.assertEqual(self.app._log_details.winfo_manager(), "pack")
        self.app.b_details.invoke()
        self.assertFalse(self.app._log_details.winfo_manager())
        self.assertEqual(len(self.app._activity_records), 20)

    def test_page_actions_restore_sidebar_and_open_activity(self):
        if not self.app._sidebar_collapsed:
            self.app.b_focus.invoke()
        self.assertTrue(self.app._sidebar_collapsed)
        self.app._open_settings()
        self.assertFalse(self.app._sidebar_collapsed)
        self.assertEqual(self.app._active_tab, "extras")
        self.app.b_activity.invoke()
        self.assertEqual(self.app._log_details.winfo_manager(), "pack")
        self.app._set_log_details(False)
        self.app._select_tab("basic")

    def test_hidden_activity_defers_cards_and_open_updates_reuse_widgets(self):
        self.app._set_log_details(True)
        self.app.clear_log()
        self.app._set_log_details(False)
        for i in range(20):
            self.app._append_log(f"[i] 签到码: {2000 + i}")
        self.assertEqual(self.app._activity_cards, {})
        self.app._set_log_details(True)
        original = dict(self.app._activity_cards)
        self.assertEqual(len(original), 20)
        self.app._append_log("[i] 签到码: 9999")
        surviving = set(original) & set(self.app._activity_cards)
        self.assertEqual(len(surviving), 19)
        for key in surviving:
            self.assertIs(original[key], self.app._activity_cards[key])
        self.assertEqual(len(self.app._activity_cards), 20)
        self.app._set_log_details(False)

    def test_quote_color_updates_preserve_text_and_skip_layout(self):
        quote = self.app._transcript
        quote.configure(text="保留可复制的转写内容")
        self.app.update_idletasks()
        quote._textbox.tag_add("sel", "1.0", "1.2")
        with patch.object(quote, "_schedule_fit") as fit:
            quote.configure(text="保留可复制的转写内容", text_color=ui.design.TXT2)
            fit.assert_not_called()
        self.assertTrue(quote._textbox.tag_ranges("sel"))
        self.assertFalse(quote._textbox.cget("yscrollcommand"))
        self.assertFalse(quote._textbox.cget("xscrollcommand"))

    def test_scrollbar_drawing_does_not_reenter_layout(self):
        bar = self.app._page_scroll._scrollbar
        with patch.object(bar._canvas, "update_idletasks") as flush:
            bar.set(0.1, 0.8)
            bar.configure(button_color=ui.design.TXT3)
            flush.assert_not_called()
        with patch.object(bar, "_draw") as draw:
            bar.set(0.1, 0.8)
            draw.assert_not_called()
        bar.configure(button_color=ui.design.BG)

    def test_repeated_code_preserves_digit_widgets_and_copy_feedback(self):
        self.app._set_code("1234")
        with patch.object(self.app.b_copy, "configure") as copy, \
                patch.object(self.app._digit_boxes[0], "configure") as first:
            self.app._set_code("1234")
            copy.assert_not_called()
            first.assert_not_called()
            self.app._set_code("1235")
            first.assert_not_called()

    def test_new_monitor_clears_the_previous_code_before_initialization(self):
        self.app._append_log("[i] 签到码: 1234")
        release = threading.Event()
        self.addCleanup(release.set)
        self.app._worker(lambda: release.wait(2), kind="monitor")
        self.assertIsNone(self.app._code)
        self.assertEqual(self.app.b_copy.cget("state"), "disabled")
        self.assertIn("准备语音识别", self.app._transcript.cget("text"))
        release.set()
        self.pump_until(lambda: self.app._task_kind is None)

    def test_busy_task_does_not_change_monitor_controls(self):
        release = threading.Event()
        self.addCleanup(release.set)
        self.assertTrue(self.app._worker(lambda: release.wait(2), kind="login"))
        with patch.object(self.app, "save_cfg") as save:
            self.app.start_monitor()
            save.assert_not_called()
        self.assertEqual(self.app._task_kind, "login")
        self.assertEqual(self.app.b_monitor.cget("state"), "disabled")
        release.set()
        self.pump_until(lambda: self.app._task_kind is None)
        self.assertEqual(self.app.b_monitor.cget("state"), "normal")

    def test_monitor_start_stop_is_driven_by_main_thread(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def monitor():
            print("[i] ASR 就绪, Ctrl+C 停止")
            release.wait(2)

        self.assertTrue(self.app._worker(monitor, kind="monitor"))
        self.pump_until(lambda: self.app._status.cget("text") == "监控中")
        self.assertEqual(self.app._status.cget("text"), "监控中")
        self.assertEqual(self.app.b_monitor.cget("text"), "停止监控")
        self.assertEqual(self.app.b_monitor.cget("fg_color"), "transparent")
        self.assertEqual(self.app.b_monitor.cget("text_color"), ui.design.RED)
        self.assertEqual(self.app._status_chip.cget("fg_color"), ui.design.GREEN_SOFT)
        self.app.b_monitor.invoke()
        self.assertTrue(self.app.stop_event.is_set())
        self.assertEqual(self.app.b_monitor.cget("state"), "disabled")
        release.set()
        self.pump_until(lambda: self.app._task_kind is None)
        self.assertEqual(self.app._status.cget("text"), "已停止")
        self.assertEqual(self.app.b_monitor.cget("state"), "normal")
        self.assertEqual(self.app.b_monitor.cget("text"), "开始监控")
        self.assertEqual(self.app.b_monitor.cget("fg_color"), ui.design.PRIMARY)
        self.assertEqual(self.app.b_monitor.cget("border_width"), 0)

    def test_worker_failure_is_visible_and_recovers_controls(self):
        def failing_task():
            raise RuntimeError("simulated audio failure")

        self.app._worker(failing_task, kind="monitor")
        self.pump_until(lambda: self.app._task_kind is None)
        self.assertEqual(self.app._status.cget("text"), "任务异常")
        self.assertEqual(self.app.b_monitor.cget("state"), "normal")
        self.assertIn("simulated audio failure", self.app.log.get("1.0", "end"))

    def test_window_close_waits_for_monitor_cleanup(self):
        release = threading.Event()
        self.addCleanup(release.set)
        self.app._worker(lambda: release.wait(3), kind="monitor")
        try:
            with patch.object(self.app, "destroy") as destroy:
                self.app._on_close()
                self.assertTrue(self.app.stop_event.is_set())
                destroy.assert_not_called()
                self.assertEqual(self.app.b_monitor.cget("state"), "disabled")
                self.assertFalse(self.app._worker(lambda: None, kind="monitor"))
                release.set()
                self.pump_until(lambda: destroy.called)
                self.pump_until(lambda: self.app._task_kind is None)
        finally:
            self.app._closing = False
            if self.app._close_job:
                self.app.after_cancel(self.app._close_job)
                self.app._close_job = None
            self.app._finish_task("monitor", False)

    def test_dirty_state_and_password_visibility(self):
        self.app.v_user.set("changed")
        self.assertIn("未保存", self.app._save_state.cget("text"))
        self.app.v_user.set(self.app._saved_values[1])
        self.assertEqual(self.app._save_state.cget("text"), "已保存")
        self.assertEqual(self.app.e_pwd.cget("show"), "•")
        self.app._toggle_password()
        self.assertEqual(self.app.e_pwd.cget("show"), "")
        self.app._toggle_password()
        self.assertEqual(self.app.e_pwd.cget("show"), "•")

    def test_theme_switch_updates_history_and_remembers_choice(self):
        original_theme = self.app._appearance
        self.app._append_log("[错误] sample error")
        self.app.v_url.set("https://example.com/unsaved-edit")
        self.app.toggle_theme()
        try:
            target_theme = "light" if original_theme == "dark" else "dark"
            self.assertEqual(ui.ctk.get_appearance_mode().lower(), target_theme)
            self.assertEqual(self.app._appearance, target_theme)
            self.assertEqual(self.app.log.tag_cget("error", "foreground"),
                             self.app._theme_color(ui.design.RED))
            self.assertIn("sample error", self.app.log.get("1.0", "end"))
            saved = yaml.safe_load(ui.CONFIG.read_text(encoding="utf-8"))
            self.assertEqual(saved["ui"]["appearance"], target_theme)
            self.assertEqual(saved["live_url"], self.base_cfg["live_url"])
            self.assertEqual(self.app.v_url.get(), "https://example.com/unsaved-edit")
            self.assertIn("未保存", self.app._save_state.cget("text"))
        finally:
            self.app.toggle_theme()

    def test_theme_save_failure_keeps_window_usable(self):
        original_theme = self.app._appearance
        with patch.object(Path, "write_text", side_effect=PermissionError("denied")):
            self.app.toggle_theme()
        try:
            self.assertNotEqual(self.app._appearance, original_theme)
            self.assertIn("偏好保存失败", self.app._save_state.cget("text"))
            self.assertEqual(self.app.b_monitor.cget("state"), "normal")
        finally:
            self.app.toggle_theme()


    def poll_log_once(self):
        if self.app._poll_job:
            self.app.after_cancel(self.app._poll_job)
            self.app._poll_job = None
        self.app._poll_log()

    def test_log_burst_coalesces_preview_and_skips_identical_redraws(self):
        self.app._show_log_transcript("之前的内容", ui.design.TXT2, "等待下一句")
        for i in range(40):
            self.app.logline(f"…识别中: 课堂内容 {i}")
        with patch.object(ui.time, "perf_counter", return_value=0), \
                patch.object(self.app._transcript, "configure", wraps=self.app._transcript.configure) as preview, \
                patch.object(self.app._transcript_hint, "configure", wraps=self.app._transcript_hint.configure) as hint:
            self.poll_log_once()
            self.assertEqual(self.app._transcript.cget("text"), "课堂内容 39")
            self.assertEqual(self.app.log.get("1.0", "end-1c"), "")
            self.assertEqual(preview.call_count, 1)
            self.assertEqual(hint.call_count, 1)
            preview.reset_mock()
            hint.reset_mock()
            self.app.logline("…识别中: 课堂内容 39")
            self.poll_log_once()
            preview.assert_not_called()
            hint.assert_not_called()
            self.app.logline("[ASR 12:00:00] 课堂内容 39")
            self.poll_log_once()
            self.assertEqual(self.app._transcript.cget("text_color"), ui.design.TXT)
            self.assertEqual(preview.call_count, 1)
            preview.reset_mock()
            self.app.logline("[ASR 12:00:01] 课堂内容 39")
            self.poll_log_once()
            preview.assert_not_called()
        self.assertEqual(len(self.app.log.get("1.0", "end-1c").splitlines()), 2)

    def test_log_burst_preserves_final_records_codes_and_completion_order(self):
        messages = [
            "[ASR 12:00:00] 第一条最终识别",
            "…识别中: 不应写入活动记录",
            "[错误] test failure",
            "*** 签到提醒 [code] 签到码: 0123",
            "*** 签到提醒 [code] 签到码: 4567",
            "[ASR 12:00:01] 第二条最终识别",
        ]
        for message in messages:
            self.app.logline(message)
        self.app.task_q.put(("monitor", True, None))
        completed = []

        def finish(*result):
            completed.append((result, self.app.log.get("1.0", "end-1c"), self.app._code))

        with patch.object(ui.time, "perf_counter", return_value=0), \
                patch.object(self.app, "_set_code", wraps=self.app._set_code) as codes, \
                patch.object(self.app, "_finish_task", side_effect=finish):
            self.poll_log_once()
        self.assertEqual([call.args for call in codes.call_args_list], [("0123",), ("4567",)])
        self.assertEqual(len(completed), 1)
        result, history, code = completed[0]
        self.assertEqual(result, ("monitor", True, None))
        self.assertEqual(code, "4567")
        self.assertEqual([line[10:] for line in history.splitlines()], [
            "第一条最终识别", "[错误] test failure",
            "*** 签到提醒 [code] 签到码: 0123", "*** 签到提醒 [code] 签到码: 4567",
            "第二条最终识别",
        ])
        self.assertIn("asr", self.app.log.tag_names("1.10"))
        self.assertIn("error", self.app.log.tag_names("2.10"))
        self.assertIn("success", self.app.log.tag_names("3.10"))
        self.assertEqual(self.app._transcript.cget("text"), "第二条最终识别")

    def test_log_poll_yields_at_time_budget_before_task_completion(self):
        for i in range(20):
            self.app.logline(f"[i] timed-event-{i:02d}")
        self.app.task_q.put(("monitor", False, None))
        with patch.object(ui.time, "perf_counter", side_effect=[0, 0.009]), \
                patch.object(self.app, "after", wraps=self.app.after) as schedule, \
                patch.object(self.app, "_finish_task") as finish:
            self.poll_log_once()
            finish.assert_not_called()
        self.assertEqual(self.app.log_q.qsize(), 19)
        self.assertEqual(schedule.call_args.args[0], 10)
        self.assertEqual(len(self.app.log.get("1.0", "end-1c").splitlines()), 1)
        with patch.object(ui.time, "perf_counter", return_value=0), \
                patch.object(self.app, "_finish_task") as finish:
            self.poll_log_once()
            finish.assert_called_once_with("monitor", False, None)

    def test_log_poll_also_limits_the_number_of_fast_records(self):
        for i in range(1000):
            self.app.logline(f"[i] queued-event-{i:04d}")
        with patch.object(ui.time, "perf_counter", return_value=0), \
                patch.object(self.app, "after", wraps=self.app.after) as schedule:
            self.poll_log_once()
        retained = len(self.app.log.get("1.0", "end-1c").splitlines())
        self.assertGreater(retained, 0)
        self.assertLessEqual(retained, 160)
        self.assertEqual(retained + self.app.log_q.qsize(), 1000)
        self.assertEqual(schedule.call_args.args[0], 10)

    def test_completion_waits_for_log_written_during_the_queue_check(self):
        result = ("monitor", True, "synthetic failure")

        def complete_with_tail():
            self.app.logline("[错误] final worker message")
            return result

        with patch.object(self.app.task_q, "get_nowait", side_effect=complete_with_tail) as completion, \
                patch.object(self.app, "_finish_task") as finish:
            self.poll_log_once()
            finish.assert_not_called()
            self.assertFalse(self.app.log_q.empty())
            self.poll_log_once()
            finish.assert_called_once_with(*result)
            completion.assert_called_once()
        self.assertIn("final worker message", self.app.log.get("1.0", "end-1c"))

    def test_log_batch_keeps_reconnect_after_earlier_partial_preview(self):
        self.app._task_kind = "monitor"
        self.addCleanup(setattr, self.app, "_task_kind", None)
        self.app._active_audio_source = "live"
        self.app._set_code("7538")
        self.app.logline("…识别中: 尚未完成的内容")
        self.app.logline("[i] 直播状态: 正在重连")
        with patch.object(ui.time, "perf_counter", return_value=0):
            self.poll_log_once()
        self.assertFalse(self.app._has_transcript)
        self.assertEqual(self.app._status.cget("text"), "正在重连")
        self.assertNotIn("尚未完成", self.app._transcript.cget("text"))
        self.assertEqual(self.app._code, "7538")
        self.app.logline("[i] ASR 就绪 · 正在接收直播音频")
        self.app.logline("[ASR 12:00:01] 恢复后的最终内容")
        with patch.object(ui.time, "perf_counter", return_value=0):
            self.poll_log_once()
        self.assertTrue(self.app._has_transcript)
        self.assertEqual(self.app._status.cget("text"), "监控中")
        self.assertEqual(self.app._transcript.cget("text"), "恢复后的最终内容")

    def test_log_batch_trims_and_scrolls_once_and_respects_follow(self):
        self.app.v_follow.set(True)
        for i in range(45):
            self.app.logline(f"[i] batch-event-{i:03d}")
        with patch.object(ui, "MAX_LOG_LINES", 20), \
                patch.object(ui.time, "perf_counter", return_value=0), \
                patch.object(self.app.log._textbox, "insert", wraps=self.app.log._textbox.insert) as insert, \
                patch.object(self.app.log, "delete", wraps=self.app.log.delete) as trim, \
                patch.object(self.app.log, "yview_moveto", wraps=self.app.log.yview_moveto) as scroll:
            self.poll_log_once()
            self.assertEqual(insert.call_count, 1)
            self.assertEqual(trim.call_count, 1)
            scroll.assert_called_once_with(1.0)
            lines = self.app.log.get("1.0", "end-1c").splitlines()
            self.assertEqual(len(lines), 20)
            self.assertIn("batch-event-025", lines[0])
            self.assertIn("batch-event-044", lines[-1])
            self.assertEqual(self.app.log.cget("state"), "disabled")
            self.app.v_follow.set(False)
            self.addCleanup(self.app.v_follow.set, True)
            scroll.reset_mock()
            self.app.logline("[i] keep the current scroll position")
            self.poll_log_once()
            scroll.assert_not_called()

    def test_stop_button_is_serviced_while_logs_are_backlogged(self):
        release = threading.Event()
        self.addCleanup(release.set)
        self.app._worker(lambda: release.wait(3), kind="monitor")
        for i in range(1000):
            self.app.logline(f"[i] busy-event-{i:04d}")
        self.app.after_cancel(self.app._poll_job)
        self.app._poll_job = self.app.after(0, self.app._poll_log)
        pending_at_stop = []

        def click_stop():
            self.app.b_monitor.invoke()
            pending_at_stop.append(self.app.log_q.qsize())

        self.app.after(0, click_stop)
        self.pump_until(lambda: pending_at_stop)
        self.assertTrue(self.app.stop_event.is_set())
        self.assertGreater(pending_at_stop[0], 0)
        self.assertEqual(self.app._status.cget("text"), "正在停止")
        release.set()
        self.pump_until(lambda: self.app._task_kind is None)


if __name__ == "__main__":
    unittest.main()
