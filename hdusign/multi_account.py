"""One GUI, independent account controllers and process-isolated services."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import shutil
import time
import uuid

import yaml
from PySide6.QtCore import QObject, Property, Signal, Slot

from hdusign.accounts import AccountStore, protect, read_json, remove_profile, write_json
from hdusign.account_worker import ProcessServices
from hdusign.desktop_controller import Controller, read_mapping, write_text_atomic
from hdusign.runtime import application_root, resource_root
from hdusign.live import live_course_url
from hdusign.reservation_webhooks import binding, binding_url, webhook_key, listening_window, windows_overlap


class WebhookStore:
    """Only a successful HTTP/business response can create a saved endpoint."""
    def __init__(self, root, legacy_url=""):
        self.path = Path(root) / "webhooks.json"
        value = read_json(self.path)
        self.items = value.get("items", {})
        self.default = value.get("default", "")
        self.legacy_url = legacy_url

    def url(self, identity):
        item = self.items.get(identity)
        return protect(item["encrypted_url"], True) if item else ""

    def default_url(self):
        return self.url(self.default) if self.default else self.legacy_url

    def persist(self):
        write_json(self.path, {"items": self.items, "default": self.default})

    def remember_verified(self, name, url):
        identity = hashlib.sha256(url.encode("utf-8")).hexdigest()
        items = dict(self.items)
        items[identity] = {"name": name.strip(), "encrypted_url": protect(url), "verified": True}
        default = self.default
        if not default and (not self.legacy_url or self.legacy_url == url):
            default = identity
        write_json(self.path, {"items": items, "default": default})
        self.items, self.default = items, default
        return identity


class ManagedController(Controller):
    def __init__(self, manager, root, username, password="", services=None, auto_load=True):
        self.manager = manager
        self.account_username = username
        self._pending_webhook = None
        self._running_webhook = None
        self._running_course = None
        self._robot_started_at = None
        self._notification_route = None
        self._launch_webhook = None
        services = services or ProcessServices(root)
        super().__init__(root, services, auto_load=False, parent=manager)
        self.account_store = manager.accounts
        self.cfg.setdefault("alert", {})["log_file"] = str(self.root / "alerts.jsonl")
        self.cfg.setdefault("code_watch", {})["log_file"] = str(self.root / "codes.jsonl")
        self._form.update(username=username, password=password)
        self.secrets.update(skl_username=username, skl_password=password)
        if getattr(services, "isolated", False):
            services.credentials = dict(self.secrets)
            services.log = self.log_queue.put
        self.apply_webhook()
        self._saved_form = dict(self._form)
        self.formChanged.emit()
        if auto_load and username:
            self._startup_timer.start(350)

    def _set_session_credentials(self, secrets):
        if getattr(self.services, "isolated", False):
            self.services.credentials = dict(secrets)
        # No writes to os.environ in the parent GUI process.

    def apply_webhook(self, preserve_edits=False):
        edits = {}
        if preserve_edits and hasattr(self, "_saved_form"):
            edits = {key: self._form[key] for key in ("webhook", "webhookName")
                     if self._form[key] != self._saved_form[key]}
        identity = self.cfg.get("webhook_binding", "")
        self.effective_webhook = (self.manager.webhooks.url(identity) if identity
                                  else self.manager.webhooks.default_url())
        record = self.manager.webhooks.items.get(identity or self.manager.webhooks.default, {})
        self._form.update(webhook=self.effective_webhook, webhookName=record.get("name", ""))
        self.cfg.setdefault("alert", {}).setdefault("webhook", {})["url"] = self.effective_webhook
        if hasattr(self, "_saved_form"):
            self._saved_form.update(webhook=self._form["webhook"], webhookName=self._form["webhookName"])
            self._form.update(edits)
            self._publish(dirty=self._form != self._saved_form)
        self.formChanged.emit()

    @Slot(result=bool)
    def save(self):
        candidate = self._form["webhook"]
        identity = self.cfg.get("webhook_binding") or self.manager.webhooks.default
        verified_name = self.manager.webhooks.items.get(identity, {}).get("name", "")
        self._form["webhook"] = self.effective_webhook
        try:
            success = super().save()
        finally:
            self._form["webhook"] = candidate
            self.formChanged.emit()
        if success:
            self._saved_form["webhookName"] = verified_name
        if success and (candidate != self.effective_webhook or self._form["webhookName"] != verified_name):
            self._publish(dirty=True, feedback="其他设置已保存；机器人名称或链接需测试成功后保存。")
        return success

    @Slot(str)
    def selectAccount(self, username):
        self.manager.activate(username)

    @Slot(str)
    def deleteAccount(self, username):
        self.manager.deleteAccount(username)

    @Slot(bool)
    def login(self, manual=False):
        if self._task:
            return
        username = self._form["username"].strip()
        if not username:
            self._feedback("请输入要登录的学号，方便隔离并保存账号。", True, "username")
            return
        target = self.manager.for_login(self, username)
        if target is not None:
            Controller.login(target, manual)

    @Slot(str, "QVariant")
    def setField(self, key, value):
        # Editing a saved identity opens a separate draft; do not mutate another
        # account's existing schedules, config or sign-in credentials.
        if key == "username" and self.account_username and str(value).strip() != self.account_username:
            if self._task:
                return
            self.manager.activate("")
            self.manager.active.setField(key, value)
            return
        super().setField(key, value)

    def _reserve_course(self, course, webhook=None):
        return self.manager.request_robot(self, course, "book")

    def _refresh_reservations(self):
        super()._refresh_reservations()
        if getattr(self.manager, "_robots_ready", False):
            self.manager.robots_changed()

    def _course_row(self, course):
        row = super()._course_row(course)
        value = (self.cfg.get("reservation_webhooks") or {}).get(row["url"], {})
        if self._task == "monitor" and row["url"] == self._monitor_url:
            value = self._running_webhook or {}
        row.update(robotName=value.get("name", "未绑定机器人"),
                   robotPending=value.get("mode") == "pending")
        return row

    def _configure_monitor(self, cfg, course, url):
        cfg.setdefault("alert", {}).setdefault("webhook", {})["url"] = binding_url(self._launch_webhook)
        cfg["_notification_route"] = dict(self._notification_route)

    def start_monitor(self, course=None):
        if self._task or self._state["closing"]:
            return False
        if not self._form["live"] and course is None:
            self._feedback("多账号监听请使用后台直播音频。", True)
            return False
        if self.manager.monitor_count() >= 5 and self._task != "monitor":
            self._feedback("已同时运行 5 个账号，请先停止其中一个。", True)
            return False
        selected = course or self._selected()
        url = live_course_url(selected) if selected else self._form["url"].strip()
        reserved = self._reserved(url)
        if self._launch_webhook is None:
            if reserved:
                value = (self.cfg.get("reservation_webhooks") or {}).get(url)
                if not self.manager.usable_robot(self, url, value, course=selected, starting=True):
                    reason = self.manager.conflict_reason(self, url, value, course=selected, starting=True)
                    value = binding(pending=True, reason=reason + " 未处理时仅本地提醒。")
                    if not self.manager.save_robot(self, url, value):
                        return False
                    self.manager.prompt_robot(self, selected, "assign", value["reason"])
                self._launch_webhook = value
            else:
                return self.manager.request_robot(self, selected, "start", url=url)
        value = self._launch_webhook
        # Recheck the actual start, including an early manual start. An old
        # process may still be finishing even after its timetable window ended.
        if value.get("mode") == "bound" and not self.manager.usable_robot(self, url, value, course=selected, starting=True):
            self._launch_webhook = None
            self.manager.prompt_robot(self, selected, "start",
                                      self.manager.conflict_reason(self, url, value, course=selected, starting=True), url=url)
            return False
        run_id = uuid.uuid4().hex
        path = self.root / "notification_routes" / (run_id + ".json")
        self._notification_route = {"path": str(path.resolve()), "run_id": run_id}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            write_json(path, {"run_id": run_id, "binding": value})
            self._running_webhook = copy.deepcopy(value)
            self._running_course = selected
            self._robot_started_at = time.time()
            self._publish(runningRobotName=value.get("name", ""), runningRobotPending=value.get("mode") != "bound")
            started = super().start_monitor(course)
        except OSError:
            self._feedback("课程机器人配置保存失败，未启动监听。", True)
            started = False
        finally:
            self._launch_webhook = None
        if not started:
            path.unlink(missing_ok=True)
            self._running_webhook, self._notification_route = None, None
            self._running_course = self._robot_started_at = None
            self._publish(runningRobotName="", runningRobotPending=False)
        else:
            self.manager.mark_used(self)
        self.manager.robots_changed()
        return started

    @Slot(str)
    def runAction(self, action):
        if action != "webhook":
            return super().runAction(action)
        if self._task or self._state["closing"]:
            return
        name, url = self._form["webhookName"].strip(), self._form["webhook"].strip()
        if not name or len(name) > 80:
            self._feedback("请先为机器人填写一个名称。", True, "webhookName")
            return
        if not self._valid_url(url):
            self._feedback("请填写有效的机器人链接。", True, "webhook")
            return
        candidate_id = hashlib.sha256(url.encode("utf-8")).hexdigest()
        if any(item["name"] == name and identity != candidate_id
               for identity, item in self.manager.webhooks.items.items()):
            self._feedback("该名称已用于其他机器人，请换一个便于区分的名称。", True, "webhookName")
            return
        if not self.save():
            return
        cfg = copy.deepcopy(self.cfg)
        cfg.setdefault("alert", {}).setdefault("webhook", {})["url"] = url
        selected = self._selected()
        cfg["notification_context"] = {"username": self.account_username,
                                       "course_name": selected.title if selected else ""}
        self._pending_webhook = (name, url)
        self._run("webhook", lambda: self.services.webhook(cfg, self.stop_event))

    def _finish(self, kind, failed, result):
        if kind == "login" and not failed and result == 0:
            try:
                saved = read_json(self.root / "live_session.json")
                code = saved.get("user_code", saved.get("username", ""))
                if code != self.account_username:
                    failed, result = True, 1
                    (self.root / "live_session.json").unlink(missing_ok=True)
                    self.log_queue.put("[错误] 浏览器登录账号与当前学号不同，请登录当前账号。")
                elif saved.get("username") != self.account_username:
                    saved["username"] = self.account_username
                    write_json(self.root / "live_session.json", saved)
            except (OSError, ValueError):
                failed, result = True, 1
        super()._finish(kind, failed, result)
        if kind == "login" and not failed and result == 0:
            self.manager.mark_used(self)
        if kind == "monitor":
            if self._notification_route:
                try:
                    Path(self._notification_route["path"]).unlink(missing_ok=True)
                except OSError:
                    pass
            self._running_webhook, self._notification_route = None, None
            self._running_course = self._robot_started_at = None
            self._publish(runningRobotName="", runningRobotPending=False)
            self.manager.robots_changed()
        if kind == "webhook":
            pending, self._pending_webhook = self._pending_webhook, None
            if not failed and result == 0 and pending:
                try:
                    identity = self.manager.webhooks.remember_verified(*pending)
                    self.manager.bind_webhook(self, identity)
                    for other in self.manager.controllers.values():
                        if other is not self:
                            other.apply_webhook(preserve_edits=True)
                    self._feedback("测试推送成功，机器人已保存并用于当前账号。")
                except (OSError, ValueError):
                    self._feedback("推送成功，但机器人保存失败，请检查目录权限。", True)
            else:
                self._feedback("测试推送未成功，链接未保存。", True)
        self.manager.changed.emit()
        if kind == "login":
            self.manager.accounts_updated()


class AccountManager(QObject):
    activeChanged = Signal()
    changed = Signal()
    closeReady = Signal()
    robotPromptChanged = Signal()

    def __init__(self, root=None, *, services_factory=None, auto_load=True, parent=None):
        super().__init__(parent)
        self.root = Path(root) if root else application_root()
        self.accounts = AccountStore(self.root)
        self._recent_use = dict(self.accounts.last_used)
        self.controllers = {}
        self._robots_ready = False
        self._robot_requests = []
        self._claims_stamp = None
        self.services_factory = services_factory
        self.auto_load = auto_load
        self.closing = False
        self.active = None
        self.base_cfg = read_mapping(self.root / "config.yaml" if (self.root / "config.yaml").exists()
                                     else resource_root() / "config.example.yaml")
        legacy_hook = (self.base_cfg.get("alert", {}).get("webhook") or {}).get("url") or ""
        self.webhooks = WebhookStore(self.root, legacy_hook)
        self._migrate_existing_account()
        for username in list(self.accounts.accounts):
            self._controller(username)
        initial = self.accounts.last if self.accounts.last in self.controllers else ""
        self._activate(initial)
        self._robots_ready = True
        self.reconcile_robots()

    def account_root(self, username):
        return self.root / "account_data" / hashlib.sha256(username.encode("utf-8")).hexdigest()

    def _migrate_existing_account(self):
        # Copy the single-account session only to its verified owner. Originals
        # remain recoverable; they are never used by multi-account workers.
        session = read_json(self.root / "live_session.json")
        username = session.get("user_code") or session.get("username")
        if not username or username not in self.accounts.accounts:
            return
        destination = self.account_root(username)
        marker = destination / ".migration-complete"
        if marker.exists():
            return
        destination.mkdir(parents=True, exist_ok=True)
        if not (destination / "config.yaml").exists():
            write_text_atomic(destination / "config.yaml", yaml.safe_dump(self.base_cfg, allow_unicode=True))
        if not (destination / "live_session.json").exists():
            shutil.copy2(self.root / "live_session.json", destination / "live_session.json")
        identity = hashlib.sha256(username.encode("utf-8")).hexdigest()
        previous = self.root / "browser_profile/accounts" / identity
        target = destination / "browser_profile/accounts" / identity
        if previous.is_dir() and not target.exists():
            try:
                shutil.copytree(previous, target)
            except OSError:
                # An old EXE may still own Chromium's database files. The new
                # account can log into sign-in again without blocking startup.
                pass
        marker.touch()

    def _controller(self, username):
        if username in self.controllers:
            return self.controllers[username]
        directory = self.account_root(username) if username else self.root / "account_data" / "draft"
        directory.mkdir(parents=True, exist_ok=True)
        cfg_path = directory / "config.yaml"
        if not cfg_path.exists():
            cfg = copy.deepcopy(self.base_cfg)
            cfg.pop("scheduled_course", None)
            cfg.pop("scheduled_courses", None)
            cfg.pop("continuous_course", None)
            cfg.pop("reservation_webhooks", None)
            cfg["live_url"] = ""
            cfg.setdefault("live_audio", {})["enabled"] = True
            # New accounts start with diagnostics enabled even if the template
            # account previously opted out after its own one-time upgrade.
            cfg.setdefault("diagnostics", {}).update(enabled=True, default_on_applied=True)
            write_text_atomic(cfg_path, yaml.safe_dump(cfg, allow_unicode=True))
        try:
            password = self.accounts.password(username) if username in self.accounts.accounts else ""
        except (OSError, ValueError):
            password = ""
        services = self.services_factory(directory) if self.services_factory else None
        controller = ManagedController(self, directory, username, password, services, self.auto_load)
        controller.changed.connect(self.changed)
        controller.closeReady.connect(self._check_closed)
        self.controllers[username] = controller
        if self._robots_ready:
            self.reconcile_robots()
        return controller

    @Property("QVariantList", notify=changed)
    def accountRows(self):
        rows = []
        for username, controller in self.controllers.items():
            if not username:
                continue
            try:
                name = read_json(controller.root / "live_session.json").get("display_name", "")
            except (OSError, ValueError):
                name = ""
            running = controller._task == "monitor"
            task_state = controller._course_views.get(controller._monitor_url, controller._state)
            next_course = controller._scheduled
            rows.append({"username": username, "name": name or username,
                         "status": task_state["status"] if running else (
                             f"已预约 {len(controller._schedules)} 门" if next_course else controller._state["status"]),
                         "course": controller._state["runningTitle"] if running else (
                             next_course.title if next_course else controller._state["courseTitle"]),
                         "active": controller is self.active,
                         "monitoring": controller._task == "monitor", "scheduled": bool(controller._scheduled)})
        return sorted(rows, key=lambda row: (row["scheduled"], self._recent_use.get(row["username"], 0)),
                      reverse=True)

    @Property("QVariantList", notify=changed)
    def webhookChoices(self):
        default = self.webhooks.items.get(self.webhooks.default, {}).get("name", "现有机器人")
        return [{"identity": "", "name": "共用默认：" + default}] + [
            {"identity": key, "name": item["name"]} for key, item in self.webhooks.items.items()]

    @Property(str, notify=changed)
    def webhookBinding(self):
        return str(self.active.cfg.get("webhook_binding", "")) if self.active else ""

    @staticmethod
    def _running_robot_window(owner):
        interval = listening_window(owner._running_course)
        if interval is None:
            return None
        start, end = interval
        if owner._robot_started_at is not None:
            start = min(start, owner._robot_started_at)
        # Completion also waits for pending sends. Until then an ending or
        # overtime worker has no predictable end time.
        if owner._state["stopping"] or time.time() >= end:
            end = float("inf")
        return start, end

    def robot_owners(self, key, controller=None, url="", *, course=None, starting=False):
        owners = []
        if not key:
            return owners
        if course is None and controller is not None:
            course = controller._reserved(url) or controller._find_course(url)
        requested = listening_window(course)
        if controller is not None and controller._notification_route and controller._monitor_url == url:
            requested = self._running_robot_window(controller)
        if starting and requested is not None:
            requested = min(time.time(), requested[0]), requested[1]
        for owner in self.controllers.values():
            values = owner.cfg.get("reservation_webhooks") or {}
            entries = [(live_course_url(c), c.title, owner._course_row(c)["fullTime"],
                        values.get(live_course_url(c)), listening_window(c)) for c in owner._schedules]
            if owner._running_webhook is not None:
                running = self._running_robot_window(owner)
                entries.append((owner._monitor_url or owner._state["runningUrl"],
                                owner._state["runningTitle"] or "正在结束的课程", "正在监听或结束中", owner._running_webhook, running))
            for course_url, title, course_time, value, interval in entries:
                if owner is controller and course_url == url:
                    continue
                if binding_url(value) and value.get("key") == key:
                    if controller is not None and not windows_overlap(requested, interval):
                        continue
                    owners.append({"account": owner.account_username, "course": title, "time": course_time})
        return owners

    def usable_robot(self, controller, url, value, *, course=None, starting=False):
        if not isinstance(value, dict):
            return False
        if value.get("mode") == "local":
            return True
        return bool(binding_url(value)) and not self.robot_owners(
            value.get("key"), controller, url, course=course, starting=starting)

    def preferred_robot(self, controller):
        url = controller.effective_webhook
        identity = controller.cfg.get("webhook_binding") or self.webhooks.default
        name = self.webhooks.items.get(identity, {}).get("name", "原有机器人")
        return binding(name, url)

    def robot_choices(self, controller, url, *, course=None, starting=False):
        choices = {}
        for item in self.webhooks.items.values():
            if item.get("verified") is not True:
                continue
            try:
                endpoint = protect(item["encrypted_url"], True)
                key = webhook_key(endpoint)
            except (OSError, ValueError, KeyError):
                continue
            if key and not self.robot_owners(key, controller, url, course=course, starting=starting):
                choices.setdefault(key, {"identity": key, "name": item["name"]})
        return sorted(choices.values(), key=lambda item: item["name"])

    def _choice_binding(self, key):
        for item in self.webhooks.items.values():
            if item.get("verified") is True:
                try:
                    url = protect(item["encrypted_url"], True)
                    if webhook_key(url) == key:
                        return binding(item["name"], url)
                except (OSError, ValueError, KeyError):
                    pass
        return None

    @Property("QVariantMap", notify=robotPromptChanged)
    def robotPrompt(self):
        if not self._robot_requests or self.closing:
            return {"visible": False, "ticket": "", "description": "", "choices": []}
        request = self._robot_requests[0]
        controller, course = request["controller"], request["course"]
        return {"visible": True, "ticket": request["ticket"],
                "description": f"账号：{controller.account_username}\n课程：{course.title if course else '手动课程'}\n\n" + request["reason"],
                "choices": self.robot_choices(controller, request["url"], course=course, starting=request["action"] == "start"),
                "cancelText": "取消本次预约" if request["action"] == "book" else "暂不处理"}

    def prompt_robot(self, controller, course, action, reason, *, url=None):
        url = url or live_course_url(course)
        if any(r["controller"] is controller and r["url"] == url for r in self._robot_requests):
            self.robotPromptChanged.emit()
            return
        self._robot_requests.append({"controller": controller, "course": course, "url": url,
                                     "action": action, "reason": reason, "ticket": uuid.uuid4().hex})
        self.robotPromptChanged.emit()

    def request_robot(self, controller, course, action, *, url=None):
        url = url or live_course_url(course)
        value = self.preferred_robot(controller)
        if self.usable_robot(controller, url, value, course=course, starting=action == "start"):
            return self._commit_robot(controller, course, url, action, value)
        reason = self.conflict_reason(controller, url, value, course=course, starting=action == "start")
        self.prompt_robot(controller, course, action, reason, url=url)
        return False

    def conflict_reason(self, controller, url, value, *, course=None, starting=False):
        value = value if isinstance(value, dict) else {}
        owners = self.robot_owners(value.get("key"), controller, url, course=course, starting=starting)
        occupied = "；".join(f"{owner['account']} 的《{owner['course']}》（{owner['time']}）" for owner in owners)
        return (f"机器人“{value.get('name', '原有机器人')}”与 {occupied} 的监听时间冲突（含课前、课后各两分钟；正在结束的任务须等待退出）。请选择其他机器人。" if owners
                else "原机器人绑定失效或尚未处理，请人工选择空闲机器人。")

    def _commit_robot(self, controller, course, url, action, value):
        if self.closing or controller._state["closing"]:
            return False
        if action == "book":
            reserved = Controller._reserve_course(controller, course, value)
            if reserved:
                self.mark_used(controller)
            return reserved
        if action == "start":
            controller._launch_webhook = value
            result = controller.start_monitor(course)
            controller._launch_webhook = None
            return result
        if controller._monitor_url == url and controller._notification_route:
            if (controller._running_webhook or {}).get("mode") == "bound":
                controller._feedback("正在推送的机器人保持不变，请结束监听后再修改。", True)
                return False
            try:
                route = controller._notification_route
                write_json(Path(route["path"]), {"run_id": route["run_id"], "binding": value})
            except OSError:
                controller._feedback("机器人配置保存失败，请重试。", True)
                return False
            controller._running_webhook = copy.deepcopy(value)
            controller._publish(runningRobotName=value["name"], runningRobotPending=False)
            controller._feedback("本门课已绑定机器人，之后的新提醒将推送到该机器人。")
            self.robots_changed()
            return True
        return self.save_robot(controller, url, value)

    def save_robot(self, controller, url, value):
        if not controller._reserved(url):
            return False
        cfg = copy.deepcopy(controller.cfg)
        cfg.setdefault("reservation_webhooks", {})[url] = copy.deepcopy(value)
        try:
            write_text_atomic(controller.config_path, yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False))
        except OSError:
            controller._feedback("预约机器人保存失败，已保留原绑定。", True)
            return False
        controller.cfg = cfg
        controller._refresh_reservations()
        return True

    @Slot(str, str)
    def chooseReservationRobot(self, username, url):
        controller = self.controllers.get(username)
        if not controller:
            return
        course = controller._reserved(url)
        if course:
            existing = (controller.cfg.get("reservation_webhooks") or {}).get(url, {})
            if existing.get("mode") == "bound" and binding_url(existing):
                controller._feedback("这门预约已绑定机器人；如需更换，请先取消这门预约再重新预约。", True)
                return
            self.prompt_robot(controller, course, "assign", "请选择这门预约使用的空闲机器人。", url=url)
        elif controller._monitor_url == url and (controller._running_webhook or {}).get("mode") != "bound":
            self.prompt_robot(controller, controller._monitor_course, "assign",
                              "当前仅保留本地提醒；选择后，之后的新提醒会推送到所选机器人。", url=url)

    @Slot(str, str)
    def confirmRobotChoice(self, ticket, identity):
        if not self._robot_requests or self._robot_requests[0]["ticket"] != ticket:
            return
        request = self._robot_requests[0]
        controller, course, url = request["controller"], request["course"], request["url"]
        value = self._choice_binding(identity)
        if value is None or not self.usable_robot(controller, url, value, course=course, starting=request["action"] == "start"):
            request["reason"] = "该机器人刚被其他预约占用或已不可用，请重新选择。"
            self.robotPromptChanged.emit()
            return
        if course and course.end + 120 <= time.time():
            controller._feedback("该课程已结束，未修改机器人。", True)
            self.cancelRobotChoice(ticket)
            return
        if self._commit_robot(controller, course, url, request["action"], value):
            self._robot_requests = [r for r in self._robot_requests if r["ticket"] != ticket]
            self.robots_changed()
            self.robotPromptChanged.emit()

    @Slot(str)
    def cancelRobotChoice(self, ticket):
        self._robot_requests = [r for r in self._robot_requests if r["ticket"] != ticket]
        self.robotPromptChanged.emit()

    @Slot(str)
    def openRobotSettings(self, ticket):
        request = next((r for r in self._robot_requests if r["ticket"] == ticket), None)
        if request is None:
            return
        # Existing pending bookings remain visible in their lists. A new booking
        # has not been committed and can be retried after testing a new robot.
        self._robot_requests = []
        self.robotPromptChanged.emit()
        self.activate(request["controller"].account_username)
        self.active.navigateSettings.emit("webhookName")

    def reconcile_robots(self):
        """Upgrade old bookings without silently assigning a different group."""
        for controller in list(self.controllers.values()):
            for course in controller._schedules:
                url = live_course_url(course)
                existing = (controller.cfg.get("reservation_webhooks") or {}).get(url)
                value = existing if existing is not None else self.preferred_robot(controller)
                if not self.usable_robot(controller, url, value, course=course):
                    reason = (value or {}).get("reason") or self.conflict_reason(controller, url, value, course=course)
                    value = binding(pending=True, reason=reason)
                if existing != value and not self.save_robot(controller, url, value):
                    continue
                if value.get("mode") == "pending":
                    self.prompt_robot(controller, course, "assign", value["reason"])
        self.robots_changed()

    def robots_changed(self):
        if not self._robots_ready:
            return
        stamp = tuple((c.account_username, tuple((live_course_url(course),
                       json.dumps((c.cfg.get("reservation_webhooks") or {}).get(live_course_url(course)), sort_keys=True))
                       for course in c._schedules), json.dumps(c._running_webhook, sort_keys=True),
                       c._state["stopping"], bool(c._running_course and listening_window(c._running_course)
                                                and time.time() >= listening_window(c._running_course)[1]))
                      for c in self.controllers.values())
        if stamp == self._claims_stamp:
            return
        self._claims_stamp = stamp
        self._robot_requests = [r for r in self._robot_requests if r["controller"] in self.controllers.values()
                                and (r["action"] in ("book", "start") or r["controller"]._reserved(r["url"])
                                     or r["controller"]._monitor_url == r["url"])]
        self.changed.emit()
        self.robotPromptChanged.emit()

    def monitor_count(self):
        return sum(controller._task == "monitor" for controller in self.controllers.values())

    def accounts_updated(self):
        for controller in self.controllers.values():
            controller.changed.emit()

    @Slot(str)
    def activate(self, username):
        self._activate(username, record_use=True)

    def mark_used(self, controller):
        username = controller.account_username
        if not username:
            return
        # Keep successive user actions ordered even with equal timestamps or a
        # clock adjustment. Unsaved accounts only keep their order in memory.
        at = max(time.time(), max(self._recent_use.values(), default=0) + 0.000001)
        self._recent_use[username] = at
        try:
            self.accounts.remember_use(username, at)
        except (OSError, ValueError):
            controller._feedback("账号排序已更新，但最近使用记录未保存，重启后可能恢复原顺序。", True)
        self.changed.emit()

    def _activate(self, username, *, record_use=False):
        if self.closing:
            return
        self.active = self._controller(username)
        if not username and not self.active._task:
            Controller.setField(self.active, "username", "")
            Controller.setField(self.active, "password", "")
        if record_use:
            self.mark_used(self.active)
        self.activeChanged.emit()
        self.changed.emit()

    def for_login(self, source, username):
        target = self._controller(username)
        if target._task:
            source._feedback("该账号正在运行任务，请切换到该账号查看。", True)
            return None
        if source is not target:
            for key, value in source._form.items():
                if key not in ("url", "webhook", "webhookName"):
                    target._form[key] = value
            target._form["username"] = username
            target.formChanged.emit()
        self._activate(username)
        return target

    @Slot(str)
    def assignWebhook(self, identity):
        if self.active and not self.active._task:
            try:
                self.bind_webhook(self.active, identity)
            except (OSError, ValueError):
                self.active._feedback("机器人设置保存失败。", True)

    def bind_webhook(self, controller, identity):
        if identity and identity not in self.webhooks.items:
            raise ValueError("机器人不存在")
        cfg = copy.deepcopy(controller.cfg)
        cfg["webhook_binding"] = identity
        # Persist the binding, not an untested field edit.
        cfg.setdefault("alert", {}).setdefault("webhook", {})["url"] = (
            self.webhooks.url(identity) if identity else self.webhooks.default_url())
        write_text_atomic(controller.config_path, yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False))
        controller.cfg = cfg
        controller.apply_webhook()
        self.changed.emit()

    @Slot(str)
    def setDefaultWebhook(self, identity):
        if identity not in self.webhooks.items:
            return
        previous = self.webhooks.default
        try:
            self.webhooks.default = identity
            self.webhooks.persist()
        except OSError:
            self.webhooks.default = previous
            self.active._feedback("默认机器人保存失败。", True)
            return
        for controller in self.controllers.values():
            if not controller.cfg.get("webhook_binding"):
                controller.apply_webhook(preserve_edits=True)
        self.changed.emit()
        self.active._feedback("默认机器人已更新，运行中的任务下次启动时生效。")

    @Slot(str)
    def stopAccount(self, username):
        controller = self.controllers.get(username)
        if controller:
            controller.stop()

    @Slot()
    def stopAll(self):
        for controller in self.controllers.values():
            controller.stop()

    @Slot(str)
    def deleteAccount(self, username):
        controller = self.controllers.get(username)
        if controller and controller._task:
            self.active._feedback("请先停止该账号的任务，再删除账号。", True)
            return
        try:
            if controller:
                controller.shutdown()
            directory = self.account_root(username)
            remove_profile(self.root, directory.relative_to(self.root))
            self.accounts.delete(username)
            self.controllers.pop(username, None)
            self._recent_use.pop(username, None)
            self.robots_changed()
            if controller is self.active:
                self.activate("")
            self.changed.emit()
            self.accounts_updated()
            self.active._feedback("账号、预约和对应浏览器登录状态已删除。")
        except (OSError, ValueError):
            self.active._feedback("删除未完成，请关闭该账号的浏览器窗口后重试。", True)

    @Slot(result=bool)
    def requestClose(self):
        self.closing = True
        self.robotPromptChanged.emit()
        for controller in self.controllers.values():
            controller.requestClose()
        return not any(controller._task for controller in self.controllers.values())

    def _check_closed(self):
        if self.closing and not any(c._task for c in self.controllers.values()):
            self.closeReady.emit()

    def shutdown(self):
        for controller in self.controllers.values():
            controller.shutdown()
