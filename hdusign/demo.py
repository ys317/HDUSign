"""Offline demonstration data, never connected to login, audio or notifications."""
from __future__ import annotations

import json
import time

import yaml

from hdusign.desktop_controller import Controller
from hdusign.live import LiveCourse, live_course_url
from hdusign.runtime import resource_root


def demo_courses():
    now = time.time()
    return [
        LiveCourse("123", "计算机网络", now - 600, now + 2100,
                   teacher="王老师", classroom="教学楼 201", section="第2节", tecl_id="456"),
        LiveCourse("124", "数据库原理", now + 3600, now + 6300, teacher="李老师"),
    ]


def prepare_demo(root):
    cfg = yaml.safe_load((resource_root() / "config.example.yaml").read_text(encoding="utf-8"))
    cfg["live_url"] = live_course_url(demo_courses()[0])
    cfg["alert"]["webhook"]["url"] = ""
    cfg["rules"]["semantic"]["enabled"] = False
    cfg.setdefault("auto_sign", {})["enabled"] = False
    cfg["ui"] = {"appearance": "dark"}
    (root / "config.yaml").write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    (root / "secrets_local.json").write_text(json.dumps({
        "skl_username": "2026001001", "skl_password": "demo-password",
    }), encoding="utf-8")


class DemoServices:
    def monitor(self, cfg, stop):
        for line in (
            "[i] ASR 就绪 · 正在接收直播音频",
            "[ASR 10:00:01] 同学们，今天继续学习上一节的内容。",
            "[ASR 10:00:04] 现在开始签到，签到码是二三三零。",
            "[i] 签到提醒：检测到签到码: 2330",
            "[i] 已识别签到码，请在课堂页面自行确认。",
        ):
            print(line)
        stop.wait()
        return 0

    def courses(self, cfg, stop):
        return demo_courses()

    def login(self, *args):
        return 0

    def _offline(self, *args):
        print("[i] 演示模式：未调用外部服务。")
        return 0

    locate = signin = webhook = _offline


class DemoController(Controller):
    def __init__(self, root):
        super().__init__(root, DemoServices(), auto_load=False)
        self.set_courses(demo_courses())
        self.selectCourse(live_course_url(self._courses[0]))

    def populate_monitor(self):
        """A frozen snapshot for repeatable screenshots; no worker or stdout capture."""
        self._publish(status="监控中", statusKind="active", elapsed="00:02:18",
                      actionText="停止监控", actionIcon="stop", destructive=True,
                      monitoring=True, busy=True)
        self.consume([
            "[ASR 10:00:04] 现在开始签到，签到码是二三三零。",
            "[i] 签到提醒：检测到签到码: 2330",
            "[i] 演示数据，未执行签到或通知。",
        ])

    def openCourse(self):
        self._feedback("演示模式：未打开外部页面。")
