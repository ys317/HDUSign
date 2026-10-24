"""Alerting: console highlight + JSONL log + 企业微信机器人, with dedup."""
from __future__ import annotations

import json
import threading
import time
import urllib.request
from typing import Dict, Iterable


class Alerter:
    def __init__(self, log_file: str = "alerts.jsonl", dedup_seconds: float = 90,
                 webhook_url: str = "", webhook_levels: Iterable[str] = ("high", "code"),
                 clock=None, student_name: str = "", course_name: str = "", webhook_resolver=None):
        self.log_file = log_file
        self.dedup_seconds = dedup_seconds
        self.webhook_url = webhook_url.strip()
        self.webhook_levels = tuple(webhook_levels)
        self.clock = clock or time.time  # file replay passes the audio position
        self._last_fire: Dict[str, float] = {}
        self.student_name = student_name
        self.course_name = course_name
        self.webhook_resolver = webhook_resolver

    def _webhook(self):
        return self.webhook_resolver() if self.webhook_resolver is not None else self.webhook_url

    def notify(self, text: str, level: str, reason: str) -> bool:
        key = f"{level}|{text.strip()[:40]}"
        now = self.clock()
        last = self._last_fire.get(key)
        if last is not None and now - last < self.dedup_seconds:
            return False
        self._last_fire[key] = now

        ts = time.strftime("%H:%M:%S")
        print(f"\n{'!' * 60}")
        print(f"*** 签到提醒 [{level}] {ts}: {reason}")
        print(f"*** 原文: {text}")
        print(f"{'!' * 60}")
        if self._webhook() and level in self.webhook_levels:
            # Finish sends before worker exit releases the course's robot claim.
            threading.Thread(target=self._send_wechat, args=(ts, level, reason, text),
                             daemon=False).start()
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": now, "time": ts, "level": level,
                                    "reason": reason, "text": text},
                                   ensure_ascii=False) + "\n")
        except OSError:
            pass
        return True

    def _send_wechat(self, ts: str, level: str, reason: str, text: str) -> bool:
        webhook_url = self._webhook()
        if not webhook_url:
            return False
        label = {"high": "签到", "medium": "疑似签到", "code": "签到码", "semantic": "语义",
                 "test": "测试"}.get(level, level)
        if level == "code":
            body = f"**【{label}】{reason}**\n> 时间: {ts}\n> 原句: {text}"
        else:
            body = (f"**【{label}】{reason}**\n"
                    f"> 时间: {ts}\n> 原句: {text}")
        # Context belongs to this monitor instance and is independent of later
        # UI account selections, including while a push is in flight.
        def label_text(value):
            value = " ".join(str(value).split())[:120]
            for char in ("\\", "*", "_", "[", "]", "(", ")", "<", ">", "`", "#", "|"):
                value = value.replace(char, "\\" + char)
            return value
        if self.student_name or self.course_name:
            title, details = body.split("\n", 1)
            body = (title + "\n> 姓名：" + label_text(self.student_name or "未获取，请重新登录")
                    + "\n> 课程：" + label_text(self.course_name or "未选择课程") + "\n" + details)
        payload = {"msgtype": "markdown", "markdown": {"content": body}}
        req = urllib.request.Request(
            webhook_url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                out = json.loads(resp.read().decode("utf-8"))
                if not isinstance(out, dict) or type(out.get("errcode")) is not int or out["errcode"] != 0:
                    print("[warn] 企业微信未确认推送成功，请检查机器人设置。")
                    return False
                return True
        except Exception as e:  # noqa: BLE001
            print("[warn] 企业微信推送失败，请检查链接和网络。")
            return False
