"""Qt presentation state and task coordination. No Tk or rendering dependencies.

Workers receive immutable snapshots and deliver results through queues. Only the
GUI thread mutates models; partial transcripts are coalesced once per UI batch.
"""
from __future__ import annotations

import copy
from dataclasses import asdict
import datetime as dt
import json
import math
from pathlib import Path
import queue
import re
import threading
import time
from contextlib import redirect_stderr, redirect_stdout, nullcontext
from urllib.parse import urlparse

import yaml
from PySide6.QtCore import QAbstractListModel, QModelIndex, QObject, Property, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtCore import QUrl
from PySide6.QtQml import QQmlPropertyMap

from hdusign import __version__
from hdusign.accounts import AccountStore, load_credentials, protect, session_credentials
from hdusign.live import LiveCourse, LiveError, live_course_url, parse_live_origin, parse_live_page
from hdusign.runtime import application_root, resource_root

ZONE = dt.timezone(dt.timedelta(hours=8))
from hdusign.lessons import EARLY_START, LATE_END

START_DELAY = -EARLY_START

COURSE_VIEW = dict(status="未开始", statusKind="idle", elapsed="00:00:00",
                   timerLabel="已运行时长", transcript="等待开始",
                   transcriptHint="选择课程后开始监控。", transcriptFinal=False,
                   code="", copied=False)


class Rows(QAbstractListModel):
    """Bounded, incrementally updated data for QML's recycled ListView delegates."""
    EntryRole = Qt.ItemDataRole.UserRole + 1
    countChanged = Signal()

    def __init__(self, limit=1500, parent=None):
        super().__init__(parent)
        self.items = []
        self.limit = limit

    def roleNames(self):
        return {self.EntryRole: b"entry"}

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.items)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if index.isValid() and 0 <= index.row() < len(self.items) and role == self.EntryRole:
            return self.items[index.row()]
        return None

    @Property(int, notify=countChanged)
    def count(self):
        return len(self.items)

    def reset(self, items=()):
        self.beginResetModel()
        self.items = list(items)[-self.limit:]
        self.endResetModel()
        self.countChanged.emit()

    def append(self, items):
        incoming = list(items)[-self.limit:]
        if not incoming:
            return
        excess = max(0, len(self.items) + len(incoming) - self.limit)
        if excess:
            self.beginRemoveRows(QModelIndex(), 0, excess - 1)
            del self.items[:excess]
            self.endRemoveRows()
        start = len(self.items)
        self.beginInsertRows(QModelIndex(), start, start + len(incoming) - 1)
        self.items.extend(incoming)
        self.endInsertRows()
        self.countChanged.emit()

    def update(self, row, value):
        if self.items[row] != value:
            self.items[row] = value
            self.dataChanged.emit(self.index(row), self.index(row), [self.EntryRole])


class Services:
    """Existing application services, imported only when explicitly invoked."""
    def monitor(self, cfg, stop):
        from hdusign.monitor import cmd_run
        return cmd_run(cfg, stop)

    def courses(self, cfg, stop):
        from hdusign.live import list_live_courses
        options = dict(cfg.get("live_audio") or {})
        options.setdefault("continuous", True)
        cfg = dict(cfg, live_audio=options)
        return list_live_courses(cfg, stop=stop)

    def login(self, url, stop, credentials, manual=False):
        from hdusign.live import login_live
        return login_live(url, stop, switch_account=manual,
                          credentials=None if manual else credentials)



    def webhook(self, cfg, stop):
        from hdusign.monitor import cmd_webhook_test
        return cmd_webhook_test(cfg)


class QueueWriter:
    def __init__(self, target):
        self.target = target

    def write(self, text):
        for line in text.splitlines():
            if line.strip():
                self.target.put(line.rstrip())
        return len(text)

    def flush(self):
        pass


def read_mapping(path):
    if not path.exists():
        return {}
    data = (json.loads(path.read_text(encoding="utf-8-sig")) if path.suffix == ".json"
            else yaml.safe_load(path.read_text(encoding="utf-8-sig")))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} 必须是键值配置")
    return data


def write_text_atomic(path, text):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


class Controller(QObject):
    changed = Signal()
    formChanged = Signal()
    closeReady = Signal()
    navigateSettings = Signal(str)
    navigateCourses = Signal()

    def __init__(self, root=None, services=None, *, auto_load=True, parent=None):
        super().__init__(parent)
        self.root = Path(root) if root else application_root()
        self.services = services or Services()
        self.config_path = self.root / "config.yaml"
        self.secrets_path = self.root / "secrets_local.json"
        self._state = dict(version=f"HDUSign v{__version__}", dark=True, status="未开始",
                           statusKind="idle", elapsed="00:00:00", timerLabel="已运行时长",
                           transcript="等待开始", transcriptHint="选择课程后开始监控。",
                           transcriptFinal=False, code="", feedback="", errorField="",
                           feedbackError=False, courseHint="登录后读取今明直播课。",
                           selectedUrl="", courseTitle="", courseSubtitle="",
                           courseLoadState="unloaded", courseError="",
                           actionText="开始监控", actionIcon="play",
                           destructive=False, busy=False, monitoring=False, stopping=False,
                           closing=False, needsLogin=False, copied=False, scheduled=False,
                           accountMonitoring=False, reservationCount=0, runningTitle="", runningUrl="",
                           runningRobotName="", runningRobotPending=False,
                           dirty=False)
        error = ""
        try:
            self.cfg = read_mapping(self.config_path if self.config_path.exists()
                                    else resource_root() / "config.example.yaml")
            self.account_store = AccountStore(self.root)
            self.secrets = load_credentials(self.root, use_session=False)
            # Migrate a legacy plaintext password without declaring it verified.
            legacy = read_mapping(self.secrets_path)
            if "skl_password" in legacy:
                legacy["skl_password_dpapi"] = protect(str(legacy.pop("skl_password")))
                write_text_atomic(self.secrets_path, json.dumps(legacy, ensure_ascii=False, indent=2))
        except (OSError, ValueError, yaml.YAMLError) as exc:
            self.cfg, self.secrets = {}, {}
            self.account_store = None
            error = f"读取设置失败：{exc}"
        if not error and (self.cfg.get("diagnostics") or {}).get("default_on_applied") is not True:
            # Enable existing accounts once on upgrade. Persist the marker with
            # the setting so a later explicit opt-out survives every restart.
            upgraded = copy.deepcopy(self.cfg)
            upgraded.setdefault("diagnostics", {}).update(enabled=True, default_on_applied=True)
            try:
                write_text_atomic(self.config_path, yaml.safe_dump(upgraded, allow_unicode=True, sort_keys=False))
            except OSError:
                error = "诊断默认设置保存失败，已保留原设置；请检查配置目录写入权限。"
            else:
                self.cfg = upgraded
        self._state["dark"] = self.cfg.get("ui", {}).get("appearance", "dark") != "light"
        self._form = dict(
            url=str(self.cfg.get("live_url", "")),
            username=str(self.secrets.get("skl_username", "")),
            password=str(self.secrets.get("skl_password", "")),
            remember=True,
            webhookName="",
            webhook=str(self.cfg.get("alert", {}).get("webhook", {}).get("url", "") or ""),
            live=bool(self.cfg.get("live_audio", {}).get("enabled", True)),
            semantic=bool(self.cfg.get("rules", {}).get("semantic", {}).get("enabled", False)),
            diagnostic=bool(self.cfg.get("diagnostics", {}).get("enabled", True)),
            rules="\n".join(str(s) for s in self.cfg.get("rules", {}).get("strong", [])))
        self._login_snapshot = None
        self._saved_form = dict(self._form)
        self._state['selectedUrl'] = self._form['url'].strip()
        # Each QML dependency observes its own key. A timer/transcript update
        # must not invalidate the whole page or reset an unrelated text editor.
        self._view_state = QQmlPropertyMap.create(self)
        self._view_form = QQmlPropertyMap.create(self)
        for key, value in self._state.items():
            self._view_state.insert(key, value)
        self._sync_form()
        self._view_state.freeze()
        self._view_form.freeze()
        self.formChanged.connect(self._sync_form)
        self.course_model = Rows(1000, self)
        self.log_model = Rows(1500, self)
        self.activity_model = Rows(20, self)
        self._courses = []
        self._schedules = []
        self._monitor_url = None
        self._monitor_course = None
        self._course_views = {}
        self._course_logs = {}
        self._course_activity = {}
        self._task = ""
        self.worker = None
        self.stop_event = threading.Event()
        self.log_queue = queue.Queue()
        self.result_queue = queue.Queue()
        self._completion = None
        self._started = None
        self._has_transcript = False
        self._copy_timer = QTimer(self)
        self._copy_timer.setSingleShot(True)
        self._copy_timer.timeout.connect(lambda: self._publish(copied=False))
        self._drain_timer = QTimer(self)
        self._drain_timer.setInterval(33)
        self._drain_timer.timeout.connect(self.drain)
        self._drain_timer.start()
        self._clock = QTimer(self)
        self._clock.setInterval(1000)
        self._clock.timeout.connect(self.tick)
        self._clock.start()
        self._restore_schedule()
        if error:
            self._feedback(error, True)
        self._startup_timer = QTimer(self)
        self._startup_timer.setSingleShot(True)
        self._startup_timer.timeout.connect(self.load_saved_courses)
        if auto_load:
            self._startup_timer.start(350)

    @Property(QObject, constant=True)
    def state(self):
        return self._view_state

    @Property(QObject, constant=True)
    def form(self):
        return self._view_form

    @Property(QObject, constant=True)
    def courses(self):
        return self.course_model

    @Property(QObject, constant=True)
    def logs(self):
        return self.log_model

    @Property(QObject, constant=True)
    def activity(self):
        return self.activity_model

    @property
    def _scheduled(self):
        """The next reservation, for account summaries and older integrations."""
        return self._schedules[0] if self._schedules else None

    @Property("QVariantList", notify=changed)
    def reservations(self):
        return [self._course_row(course) for course in self._schedules]

    def _find_course(self, url):
        return next((c for c in [*self._courses, *self._schedules,
                                 *([self._monitor_course] if self._monitor_course else [])]
                     if live_course_url(c) == url), None)

    def _reserved(self, url=None):
        url = self._state["selectedUrl"] if url is None else url
        return next((c for c in self._schedules if live_course_url(c) == url), None)

    def _task_publish(self, **values):
        # A background task always owns its original course, even when the user
        # is viewing or booking another one. Only visible-course data reaches QML.
        url = self._monitor_url
        if url is None:
            self._course_views.setdefault(self._state["selectedUrl"], dict(COURSE_VIEW)).update(values)
            self._publish(**values)
            return
        self._course_views.setdefault(url, dict(COURSE_VIEW)).update(values)
        if url == self._state["selectedUrl"]:
            self._publish(**values)
        else:
            self.changed.emit()

    def _show_course(self):
        url = self._state["selectedUrl"]
        self._publish(**self._course_views.get(url, COURSE_VIEW))
        self.log_model.reset(self._course_logs.get(url, ()))
        self.activity_model.reset(self._course_activity.get(url, ()))
        self._show_schedule()
        self._action()

    def _refresh_reservations(self):
        for row, course in enumerate(self._courses):
            self.course_model.update(row, self._course_row(course))
        self._publish(reservationCount=len(self._schedules))
        self.changed.emit()
        self._action()

    def _publish(self, **values):
        if "selectedUrl" in values:
            values.update(self._course_details(values["selectedUrl"]))
        changed = {key: value for key, value in values.items() if self._state.get(key) != value}
        if changed:
            self._state.update(changed)
            for key, value in changed.items():
                self._view_state.insert(key, value)
            self.changed.emit()

    def _sync_form(self):
        for key, value in self._form.items():
            if self._view_form.value(key) != value:
                self._view_form.insert(key, value)

    def _feedback(self, message, error=False, field=""):
        self._publish(feedback=message, feedbackError=error, errorField=field)
        if field:
            self.navigateSettings.emit(field)

    @Slot(str, "QVariant")
    def setField(self, key, value):
        if key not in self._form or self._state["closing"]:
            return
        if self._task and key in ("username", "password", "remember"):
            return
        value = bool(value) if key in ("live", "semantic", "remember", "diagnostic") else str(value)
        if self._form[key] == value:
            return
        if key in ("username", "password") and self._scheduled:
            if not self._persist_schedule([]):
                return
            self._schedules = []
            self._refresh_reservations()
        self._form[key] = value
        if key in ("username", "password"):
            self._courses = []
            self.course_model.reset()
            if self._form["live"]:
                self._form["url"] = ""
            self._publish(needsLogin=True, selectedUrl="", courseLoadState="unloaded", courseError="",
                          courseHint="账号已修改，请登录并读取课程。")
        elif key == "url":
            self._publish(selectedUrl=value.strip())
            self._show_course()
        self.formChanged.emit()
        self._publish(dirty=self._form != self._saved_form, feedback="有未保存的更改",
                      feedbackError=False, errorField="")
        self._action()

    @Property("QVariantList", notify=changed)
    def savedAccounts(self):
        return sorted(self.account_store.accounts) if self.account_store else []

    @Slot(str)
    def selectAccount(self, username):
        if self._task or self._state["closing"] or not self.account_store:
            return
        try:
            password = self.account_store.password(username) if username else ""
        except (OSError, ValueError, KeyError):
            self._feedback("保存的密码无法读取，请重新输入并登录。", True)
            return
        self.setField("username", username)
        self.setField("password", password)
        self.setField("remember", True)
        self._feedback("已填入账号，点击登录即可。" if username else "请输入新账号并登录。")

    @Slot(str)
    def deleteAccount(self, username):
        if self._task or self._state["closing"] or not self.account_store:
            return
        if username not in self.account_store.accounts:
            return
        if self._scheduled:
            if not self._persist_schedule([]):
                return
            self._schedules = []
        try:
            self.account_store.delete(username)
        except (OSError, ValueError):
            self._feedback("删除未完成，请关闭该账号的浏览器窗口后重试。", True)
            return
        if self._form["username"].strip() == username:
            self.setField("username", "")
            self.setField("password", "")
            self.secrets.update(skl_username="", skl_password="")
            session_credentials(self.root, self.secrets)
        self.changed.emit()
        self._feedback("账号及其浏览器登录状态已删除。")

    @Slot(result=bool)
    def save(self):
        cfg, secrets = copy.deepcopy(self.cfg), copy.deepcopy(self.secrets)
        cfg["live_url"] = self._form["url"].strip()
        cfg.setdefault("ui", {})["appearance"] = "dark" if self._state["dark"] else "light"
        cfg.setdefault("live_audio", {})["enabled"] = self._form["live"]
        cfg.setdefault("alert", {}).setdefault("webhook", {})["url"] = self._form["webhook"].strip()
        cfg.pop("auto_sign", None)
        cfg.pop("location", None)
        cfg.setdefault("rules", {}).setdefault("semantic", {})["enabled"] = self._form["semantic"]
        cfg.setdefault("diagnostics", {})["enabled"] = self._form["diagnostic"]
        cfg["rules"]["strong"] = [s.strip() for s in self._form["rules"].splitlines() if s.strip()]
        self._schedule_config(cfg, self._schedules)
        secrets.update(skl_username=self._form["username"].strip(), skl_password=self._form["password"])
        try:
            write_text_atomic(self.config_path, yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False))
            disk_secrets = read_mapping(self.secrets_path)
            # General settings never save unverified edits to account credentials.
            disk_secrets.update({k: v for k, v in secrets.items()
                                 if k not in ("skl_username", "skl_password", "skl_password_dpapi")})
            write_text_atomic(self.secrets_path, json.dumps(disk_secrets, ensure_ascii=False, indent=2))
            self._set_session_credentials(secrets)
        except OSError as exc:
            self._feedback(f"保存失败：{exc.strerror or '无法写入配置'}", True)
            return False
        self.cfg, self.secrets = cfg, secrets
        self._saved_form = dict(self._form)
        self._publish(dirty=False, feedback="设置已保存", feedbackError=False, errorField="")
        return True

    def _set_session_credentials(self, secrets):
        session_credentials(self.root, secrets)

    @Slot()
    def toggleTheme(self):
        dark = not self._state["dark"]
        self._publish(dark=dark)
        cfg = copy.deepcopy(self.cfg)
        cfg.setdefault("ui", {})["appearance"] = "dark" if dark else "light"
        try:
            write_text_atomic(self.config_path, yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False))
        except OSError:
            self._feedback("主题已切换，但偏好保存失败", True)
        else:
            self.cfg = cfg

    def _course_row(self, course):
        start, end = (dt.datetime.fromtimestamp(value, ZONE) for value in (course.start, course.end))
        now = time.time()
        today = dt.datetime.now(ZONE).date()
        day = "今天" if start.date() == today else "明天" if start.date() == today + dt.timedelta(days=1) else start.strftime("%m-%d")
        phase = "进行中" if course.start <= now < course.end else "待开课" if now < course.start else "已结束"
        return dict(url=live_course_url(course), title=course.title, time=start.strftime("%H:%M"),
                    summary=" · ".join(filter(None, (phase, day, course.teacher))),
                    detail=" · ".join(filter(None, (course.classroom, course.section))),
                    active=phase == "进行中", start=course.start, end=course.end,
                    reserved=bool(self._reserved(live_course_url(course))),
                    monitoring=self._task == "monitor" and self._monitor_url == live_course_url(course),
                    fullTime=f"{start:%m-%d %H:%M}–{end:%H:%M}")

    def _course_details(self, url):
        course = self._find_course(url)
        if course is None:
            return dict(courseTitle="", courseSubtitle="")
        row = self._course_row(course)
        return dict(courseTitle=course.title, courseSubtitle=" · ".join(
            filter(None, (row["fullTime"], course.teacher, row["detail"]))))

    def set_courses(self, courses):
        self._courses = list(courses or [])[:self.course_model.limit]
        self.course_model.reset(self._course_row(c) for c in self._courses)
        self._publish(**self._course_details(self._state["selectedUrl"]),
                      courseLoadState="ready" if self._courses else "empty", courseError="",
                      courseHint=f"找到 {len(self._courses)} 堂今明直播课，连堂课将持续监控至最后一节。" if self._courses
                      else "今明没有可选择的直播课，可刷新或在设置中填写网址。")
        self._action()

    def _selected(self):
        return self._find_course(self._form["url"].strip())

    @Slot(str)
    def selectCourse(self, url):
        if (self._task and self._task != "monitor") or self._state["closing"]:
            return
        course = self._find_course(url)
        if not course:
            return
        self._form.update(url=url, live=True)
        self.formChanged.emit()
        row = self._course_row(course)
        self._publish(selectedUrl=url, courseHint=f"{row['fullTime']}  {row['detail']}",
                      dirty=self._form != self._saved_form)
        self._show_course()

    @Slot()
    def load_saved_courses(self):
        if self._task or self._state["closing"]:
            return
        path = self.root / "live_session.json"
        if not path.exists():
            return
        try:
            saved = read_mapping(path)
        except (OSError, ValueError):
            return
        if "username" in saved and saved["username"] != self._form["username"].strip():
            self._publish(needsLogin=True, courseHint="保存的登录属于其他账号，请重新登录。")
            if self._scheduled and self._persist_schedule([]):
                self._schedules = []
                self._refresh_reservations()
            return
        self.refreshCourses()

    @Slot()
    def refreshCourses(self):
        if self._task or self._state["closing"]:
            return
        if self._state["needsLogin"]:
            self._feedback("请先登录并读取课程", True, "username")
            return
        self._publish(courseLoadState="loading", courseError="", courseHint="正在读取直播课程…")
        self._courses = []
        self.course_model.reset()
        cfg = copy.deepcopy(self.cfg)
        cfg["live_url"] = self._form["url"].strip()
        self._run("courses", lambda: self.services.courses(cfg, self.stop_event))

    @Slot(bool)
    def login(self, manual=False):
        if self._task or self._state["closing"]:
            return
        if not manual:
            for key, caption in (("username", "学号 / 账号"), ("password", "密码")):
                if not self._form[key].strip():
                    self._feedback(f"请输入{caption}", True, key)
                    return
        if not self.save():
            return
        url = self._form["url"].strip()
        try:
            parse_live_origin(url)
        except LiveError:
            url = "https://course.hdu.edu.cn/#/home"
        credentials = copy.deepcopy(self.secrets)
        self._login_snapshot = (credentials, self._form["remember"], manual)
        self._courses = []
        self.course_model.reset()
        self._form["url"] = ""
        self.formChanged.emit()
        self._publish(needsLogin=True, selectedUrl="", courseLoadState="loading", courseError="",
                      courseHint="正在登录，需要验证码时请在浏览器中完成。")
        self._run("login", lambda: self.services.login(url, self.stop_event, credentials, manual))

    def _action(self):
        selected = self._selected()
        future = selected is not None and selected.start + START_DELAY > time.time()
        monitoring = self._task == "monitor" and (
            self._monitor_url is None or self._monitor_url == self._state["selectedUrl"])
        reserved = bool(self._reserved())
        closing, stopping = self._state["closing"], self._state["stopping"]
        text = ("正在关闭…" if closing else "正在停止…" if stopping and monitoring else "停止监控" if monitoring
                else "取消预约" if reserved else "预约直播" if future else "开始监控")
        self._publish(actionText=text, actionIcon="stop" if monitoring else "clock" if future or reserved else "play",
                      destructive=monitoring or reserved, busy=bool(self._task),
                      monitoring=monitoring, scheduled=reserved, accountMonitoring=self._task == "monitor")

    def _restore_schedule(self):
        legacy = "scheduled_courses" not in self.cfg and "scheduled_course" in self.cfg
        values = self.cfg.get("scheduled_courses", [self.cfg["scheduled_course"]] if legacy else [])
        courses = {}
        for value in values if isinstance(values, list) else []:
            try:
                data = dict(value, start=float(value["start"]), end=float(value["end"]))
                course = LiveCourse(**data)
                if not math.isfinite(course.start) or not math.isfinite(course.end) or (
                        course.end <= course.start or course.end + LATE_END <= time.time()):
                    continue
                courses[live_course_url(course)] = course
            except (TypeError, ValueError, KeyError, LiveError):
                continue
        self._schedules = sorted(courses.values(), key=lambda c: c.start)
        if legacy or values != [asdict(c) for c in self._schedules]:
            self._persist_schedule(self._schedules)
        if self._schedules and not self._find_course(self._form["url"].strip()):
            url = live_course_url(self._schedules[0])
            self._form.update(url=url, live=True)
            self._saved_form = dict(self._form)
            self._publish(selectedUrl=url)
            self.formChanged.emit()
        self._refresh_reservations()
        self._show_schedule()

    @staticmethod
    def _schedule_config(cfg, courses):
        # Read the old singleton on upgrade, but never leave a stale singleton
        # that another code path could mistake for the current queue.
        cfg.pop("scheduled_course", None)
        if courses:
            cfg["scheduled_courses"] = [asdict(course) for course in courses]
        else:
            cfg.pop("scheduled_courses", None)
        bindings = cfg.get("reservation_webhooks") or {}
        retained = {live_course_url(course): bindings[live_course_url(course)]
                    for course in courses if live_course_url(course) in bindings}
        if retained:
            cfg["reservation_webhooks"] = retained
        else:
            cfg.pop("reservation_webhooks", None)

    def _persist_schedule(self, courses):
        cfg = copy.deepcopy(self.cfg)
        self._schedule_config(cfg, courses)
        try:
            write_text_atomic(self.config_path, yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False))
        except OSError:
            self._feedback("预约保存失败，请检查配置目录写入权限", True)
            return False
        self.cfg = cfg
        return True

    def _show_schedule(self):
        course = self._reserved()
        if not course or self._task == "monitor" and self._monitor_url == live_course_url(course):
            return
        row = self._course_row(course)
        self._publish(status="已预约", statusKind="waiting", timerLabel="距离监听",
                      selectedUrl=row["url"], transcript=f"等待 {course.title} 开始",
                      transcriptFinal=False, transcriptHint=f"{row['fullTime']} · 预约已保存，请保持软件运行。")
        self._action()
        self._update_countdown()

    @Slot(result=bool)
    def cancelSchedule(self):
        return self._cancel_reservation(self._state["selectedUrl"])

    @Slot(str)
    def cancelReservation(self, url):
        if not self._state["closing"]:
            self._cancel_reservation(url)

    def _cancel_reservation(self, url):
        if not self._reserved(url):
            return True
        remaining = [c for c in self._schedules if live_course_url(c) != url]
        if not self._persist_schedule(remaining):
            return False
        self._schedules = remaining
        if self._state["selectedUrl"] == url:
            self._show_course()
            self._publish(transcriptHint="预约已取消。")
        self._refresh_reservations()
        return True

    @Slot()
    def toggleMonitor(self):
        if self._task == "monitor" and self._state["monitoring"]:
            self.stop()
        elif (not self._task or self._task == "monitor") and not self._state["closing"]:
            if self._reserved():
                self.cancelSchedule()
                return
            selected = self._selected()
            if selected and selected.start + START_DELAY > time.time():
                self._reserve_course(selected)
            else:
                self.start_monitor()

    def _reserve_course(self, course, webhook=None):
        if self._state["closing"] or self._reserved(live_course_url(course)):
            return False
        previous, previous_cfg = self._schedules, copy.deepcopy(self.cfg)
        if webhook is not None:
            self.cfg.setdefault("reservation_webhooks", {})[live_course_url(course)] = copy.deepcopy(webhook)
        self._schedules = sorted([*previous, course], key=lambda c: c.start)
        if not self.save():
            self._schedules, self.cfg = previous, previous_cfg
            return False
        self._refresh_reservations()
        self._show_schedule()
        return True

    def _configure_monitor(self, cfg, course, url):
        """Managed accounts override the notification route for this one task."""

    def start_monitor(self, course=None):
        if self._task or self._state["closing"]:
            return False
        url = live_course_url(course) if course else self._form["url"].strip()
        if course or self._form["live"]:
            if self._state["needsLogin"]:
                self._feedback("账号已更改，请先登录并读取课程", True, "username")
                return False
            try:
                parse_live_page(url)
            except LiveError as exc:
                self._feedback(str(exc), True, "url")
                return False
        if not self.save():
            return False
        cfg = copy.deepcopy(self.cfg)
        cfg["live_url"] = url
        if course:
            cfg.setdefault("live_audio", {})["enabled"] = True
        selected = course or self._selected()
        cfg["notification_context"] = {"username": self._form["username"].strip(),
                                       "course_name": selected.title if selected else ""}
        cfg.pop("continuous_course", None)
        if selected:
            cfg["continuous_course"] = asdict(selected)
        self._configure_monitor(cfg, selected, url)
        previous = self._schedules
        remaining = [c for c in previous if live_course_url(c) != url]
        if remaining != previous:
            if not self._persist_schedule(remaining):
                return False
            self._schedules = remaining
        self._monitor_url, self._monitor_course = url, selected
        self._publish(runningTitle=selected.title if selected else "课程直播", runningUrl=url)
        started = self._run("monitor", lambda: self.services.monitor(cfg, self.stop_event))
        if not started:
            self._monitor_url, self._monitor_course = None, None
            if remaining != previous and self._persist_schedule(previous):
                self._schedules = previous
        self._refresh_reservations()
        return started

    @Slot()
    def stop(self):
        if self._task == "monitor" and not self.stop_event.is_set():
            self.stop_event.set()
            self._publish(stopping=True)
            self._task_publish(status="正在停止", statusKind="waiting")
            self._action()

    @Slot(str)
    def runAction(self, action):
        if self._task or self._state["closing"] or action != "webhook":
            return
        if action == "webhook" and not self._valid_url(self._form["webhook"]):
            self._feedback("请填写有效的企业微信 Webhook 地址", True, "webhook")
            return
        if not self.save():
            return
        cfg = copy.deepcopy(self.cfg)
        if action == "webhook":
            selected = self._selected()
            cfg["notification_context"] = {"username": self._form["username"].strip(),
                                           "course_name": selected.title if selected else ""}
            cfg["live_url"] = self._form["url"].strip()
        self._run(action, lambda: getattr(self.services, action)(cfg, self.stop_event))

    @staticmethod
    def _valid_url(value):
        try:
            parsed = urlparse(value.strip())
            return parsed.scheme in ("https", "http") and bool(parsed.hostname)
        except ValueError:
            return False

    @Slot()
    def openCourse(self):
        url = self._form["url"].strip()
        if self._valid_url(url):
            QDesktopServices.openUrl(QUrl(url))
        else:
            self._feedback("请填写有效的课程网址", True, "url")

    @Slot()
    def openLogs(self):
        directory = self.root / "logs"
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            self._feedback("日志目录无法创建，请检查软件目录权限。", True)
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory.resolve())))

    @Slot()
    def copyCode(self):
        if self._state["code"]:
            QGuiApplication.clipboard().setText(self._state["code"])
            self._publish(copied=True)
            self._copy_timer.start(1800)

    @Slot()
    def clearActivity(self):
        url = self._state["selectedUrl"]
        self._course_logs.pop(url, None)
        self._course_activity.pop(url, None)
        self.log_model.reset()
        self.activity_model.reset()

    def _run(self, kind, fn):
        if self._task or self._state["closing"]:
            return False
        self.stop_event.clear()
        self._task = kind
        captions = dict(monitor="正在启动", courses="读取直播课", login="登录直播",
                        webhook="测试通知")
        self._publish(stopping=False)
        publish = self._task_publish if kind == "monitor" else self._publish
        publish(status=captions[kind], statusKind="waiting")
        if kind == "monitor":
            self._started = time.monotonic()
            self._has_transcript = False
            publish(code="", elapsed="00:00:00", timerLabel="已运行时长", copied=False,
                          transcript="正在准备语音识别…", transcriptHint="初始化本地模型，请稍候。",
                          transcriptFinal=False)
        self._action()

        def work():
            result, failed = None, False
            writer = QueueWriter(self.log_queue)
            with (nullcontext() if getattr(self.services, "isolated", False) else redirect_stdout(writer)), \
                 (nullcontext() if getattr(self.services, "isolated", False) else redirect_stderr(writer)):
                try:
                    result = fn()
                    failed = type(result) is int and result != 0
                except SystemExit as exc:
                    failed = exc.code not in (None, 0)
                    result = exc.code
                    if failed:
                        writer.write(f"[错误] 任务退出：{exc.code}")
                except Exception as exc:
                    result, failed = exc, True
                    writer.write(f"[错误] {exc}")
            self.result_queue.put((kind, failed, result))

        self.worker = threading.Thread(target=work, name=f"HDUSign {kind}", daemon=True)
        self.worker.start()
        return True

    def _finish(self, kind, failed, result):
        self._task = ""
        self.worker = None
        if kind == "monitor":
            self._update_elapsed()
            self._started = None
        failure_status = dict(monitor="监控异常", courses="课程加载失败", login="登录未完成",
                              webhook="通知失败")
        self._publish(stopping=False)
        publish = self._task_publish if kind == "monitor" else self._publish
        publish(status=failure_status.get(kind, "任务异常") if failed else "已停止" if kind == "monitor" else "就绪",
                      statusKind="error" if failed else "idle")
        self._action()
        if self._state["closing"]:
            self.closeReady.emit()
            return
        if kind == "monitor":
            self._task_publish(transcriptHint="请查看活动记录后重试。" if failed else "监控已停止，可选择课程开始下一次监控。")
            self._monitor_url, self._monitor_course = None, None
            self._publish(runningTitle="", runningUrl="")
            self._refresh_reservations()
        elif kind == "courses":
            if failed:
                message = str(result) if isinstance(result, LiveError) else "读取课程失败，请检查网络后重试。"
                self._publish(courseLoadState="error", courseError=message, courseHint=message)
            else:
                self.set_courses(result)
        elif kind == "login":
            ok = not failed and result == 0
            self._publish(needsLogin=not ok, courseLoadState="loading" if ok else "unloaded",
                          courseHint="登录成功，正在读取课程…" if ok else "登录未完成，请核对账号或完成浏览器验证。")
            if ok:
                snapshot = self._login_snapshot
                if snapshot and not snapshot[2]:
                    credentials, remember, _ = snapshot
                    try:
                        if self.account_store is None:
                            raise ValueError("请先修复已保存账号文件。")
                        if remember:
                            self.account_store.save(credentials["skl_username"], credentials["skl_password"])
                        else:
                            # Do not change existing remembered accounts, but do
                            # not auto-fill a previous account on the next launch.
                            from hdusign.accounts import write_json
                            write_json(self.account_store.path,
                                       {"accounts": self.account_store.accounts, "last": ""})
                            self.account_store.last = ""
                        disk = read_mapping(self.secrets_path)
                        for key in ("skl_username", "skl_password", "skl_password_dpapi"):
                            disk.pop(key, None)
                        write_text_atomic(self.secrets_path, json.dumps(disk, ensure_ascii=False, indent=2))
                        self.changed.emit()
                    except (OSError, ValueError):
                        self._feedback("登录成功，但账号保存失败，请检查文件权限或重新输入密码。", True)
                self._login_snapshot = None
                self.navigateCourses.emit()
                self.refreshCourses()
            else:
                self._feedback("登录未完成，请核对账号或完成浏览器验证。", True, "username")
        if self._scheduled and not self._task:
            self._show_schedule()

    def _update_elapsed(self):
        if self._started is not None:
            seconds = int(time.monotonic() - self._started)
            self._task_publish(elapsed=f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}")

    def _update_countdown(self):
        course = self._reserved()
        if not course:
            return
        seconds = max(0, int(course.start + START_DELAY - time.time()))
        days, seconds = divmod(seconds, 86400)
        text = f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}"
        self._publish(elapsed=f"{days}天 {text}" if days else text)

    @Slot()
    def tick(self):
        self._update_elapsed()
        self._refresh_reservations()
        if not self._scheduled or self._state["closing"]:
            return
        self._update_countdown()
        now = time.time()
        remaining = [c for c in self._schedules if now < c.end + LATE_END]
        if len(remaining) != len(self._schedules):
            if self._persist_schedule(remaining):
                self._schedules = remaining
                self._show_course()
                self._refresh_reservations()
                self._feedback("预约课程已结束，未启动监控。")
        if self._scheduled and now >= self._scheduled.start + START_DELAY and not self._task:
            course = self._scheduled
            if not self.start_monitor(course):
                self._show_schedule()

    @Slot()
    def drain(self):
        deadline = time.perf_counter() + .006
        lines = []
        for _ in range(160):
            try:
                lines.append(self.log_queue.get_nowait())
            except queue.Empty:
                break
            if time.perf_counter() >= deadline:
                break
        self.consume(lines)
        self._drain_timer.setInterval(16 if not self.log_queue.empty() else 33 if self._task else 100)
        if self._completion is None:
            try:
                self._completion = self.result_queue.get_nowait()
            except queue.Empty:
                pass
        if self._completion is not None and self.log_queue.empty():
            completion, self._completion = self._completion, None
            self._finish(*completion)

    def consume(self, lines):
        logs, activity, updates = [], [], {}
        for raw in lines:
            line = raw.strip()
            if not line:
                continue
            if line.startswith("…识别中:"):
                self._has_transcript = True
                updates.update(transcript=line.partition(":")[2].strip(), transcriptFinal=False, transcriptHint="")
                continue
            asr = line.startswith("[ASR ")
            if asr:
                self._has_transcript = True
                updates.update(transcript=line.partition("]")[2].strip(), transcriptFinal=True, transcriptHint="")
            code = re.search(r"签到码[:：]\s*([0-9]{4})(?!\d)", line)
            if code:
                updates.update(code=code.group(1), copied=False)
            if "ASR 就绪" in line and self._task == "monitor" and not self.stop_event.is_set():
                updates.update(status="监控中", statusKind="active")
                if not self._has_transcript:
                    updates.update(transcript="等待课堂声音…", transcriptHint="正在接收课堂音频。", transcriptFinal=False)
            if line.startswith("[i] 直播状态:") and self._task == "monitor" and not self.stop_event.is_set():
                reconnecting = "正在重连" in line
                self._has_transcript = False
                updates.update(status="正在重连" if reconnecting else "正在连接", statusKind="waiting",
                               transcript="直播暂时中断，正在重新连接…" if reconnecting else "正在连接直播…",
                               transcriptFinal=False, transcriptHint="恢复收到声音后会继续识别。")
            if line.startswith("[i] 直播课间休息") and self._task == "monitor" and not self.stop_event.is_set():
                self._has_transcript = False
                updates.update(status="课间休息", statusKind="waiting",
                               transcript="等待下一节开始…", transcriptFinal=False,
                               transcriptHint="到点后继续接收直播音频，可随时停止。")
            kind = ("error" if line.startswith("[错误]") or "Traceback" in line else
                    "warning" if line.startswith(("[!]", "[warn]")) else
                    "success" if line.startswith("[OK]") or "签到提醒" in line else "asr" if asr else "info")
            text = re.sub(r"^\[(?:ASR [^\]]+|i|OK)\]\s*", "", line)
            entry = dict(time=time.strftime("%H:%M:%S"), text=text, kind=kind)
            logs.append(entry)
            if not asr and (code or "签到提醒" in line):
                activity.append(entry)
        url = self._monitor_url if self._monitor_url is not None else self._state["selectedUrl"]
        for storage, entries, limit in ((self._course_logs, logs, self.log_model.limit),
                                        (self._course_activity, activity, self.activity_model.limit)):
            storage[url] = [*storage.get(url, ()), *entries][-limit:]
        if url == self._state["selectedUrl"]:
            self.log_model.append(logs)
            self.activity_model.append(activity)
        self._task_publish(**updates)

    @Slot(result=bool)
    def requestClose(self):
        self._publish(closing=True)
        self.stop_event.set()
        self._action()
        if not self._task:
            return True
        self._publish(status="正在关闭", statusKind="waiting")
        return False

    def shutdown(self):
        self.stop_event.set()
        for timer in (self._clock, self._drain_timer, self._copy_timer, self._startup_timer):
            timer.stop()
