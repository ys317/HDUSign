"""HDUSign desktop console.

  python -m hdusign               # 图形界面
  HDUSign.exe --sign 2330         # 打包后自动签到入口（内部使用）
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import math
import queue
import re
import sys
import threading
import time
import webbrowser
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tkinter import TclError, font as tkfont
from urllib.parse import urlparse

import customtkinter as ctk
import yaml

from hdusign import __version__, ui as design
from hdusign.location import DEFAULT_LAT, DEFAULT_LNG, Location, LocationError, get_current_location
from hdusign.live import LiveCourse
from hdusign.ui import Entry, NotionMenu, QuoteText, ScrollableFrame, Switch, Textbox
from hdusign.runtime import application_root, resource_root

APP_ROOT = application_root()

CONFIG = APP_ROOT / "config.yaml"
SECRETS = APP_ROOT / "secrets_local.json"
RESOURCE_ROOT = resource_root()

APP_VERSION = f"v{__version__}"
MAX_LOG_LINES = 1500
SCHEDULE_START_DELAY = 2.0
SIDEBAR_WIDTH = 240
SIDEBAR_COLLAPSED = 56
CANVAS_MAX_WIDTH = 840


def load_cfg() -> dict:
    path = CONFIG if CONFIG.exists() else RESOURCE_ROOT / "config.example.yaml"
    if not path.exists():
        return {}
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(cfg, dict):
        raise ValueError("配置文件内容应为键值配置")
    return cfg


def load_secrets() -> dict:
    if SECRETS.exists():
        return json.loads(SECRETS.read_text(encoding="utf-8"))
    return {"skl_username": "", "skl_password": ""}


class QueueWriter:
    """把后台输出送到主线程，不直接操作 Tk 控件。"""

    def __init__(self, q: queue.Queue):
        self.q = q

    def write(self, s):
        if s and s.strip():
            for line in s.splitlines():
                if line.strip():
                    self.q.put(line.rstrip())
        return len(s)

    def flush(self):
        pass


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        design.configure_ui_fonts(self)
        self.cfg = load_cfg()
        self._appearance = (self.cfg.get("ui") or {}).get("appearance", "dark")
        if self._appearance not in ("light", "dark"):
            self._appearance = "dark"
        ctk.set_appearance_mode(self._appearance)
        self.configure(fg_color=design.BG)
        self.title(f"HDUSign {APP_VERSION}")
        icon = RESOURCE_ROOT / "assets" / "hdusign.ico"
        if icon.exists():
            self.iconbitmap(str(icon))
        self.geometry("1180x760")
        self.minsize(960, 600)

        self.secrets = load_secrets()
        self.log_q: queue.Queue = queue.Queue()
        self.task_q: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.stop_event = threading.Event()
        self._task_kind: str | None = None
        self._t0: float | None = None
        self._loading = True
        self._poll_job = None
        self._tick_job = None
        self._copy_job = None
        self._close_job = None
        self._closing = False
        self._code: str | None = None
        self._has_transcript = False
        self._pages = {}
        self._tabs = {}
        self._active_tab = None
        self._entries = {}
        self._field_actions = {}
        self._field_labels = {}
        self._active_audio_source = "system"
        self._monitor_ready = False
        self._live_courses = []
        self._live_course_lookup = {}
        self._scheduled_course: LiveCourse | None = None
        self._needs_live_login = False
        self._account_expanded = False
        self._account_menu = None
        self._page_menu = None
        self._activity_records = []
        self._activity_cards = {}
        self._activity_dirty = False
        self._activity_sequence = 0
        self._document_padding = None
        self._status_key = "idle"
        self._breath = False
        self._course_cards = {}
        self._sidebar_collapsed = False
        self._collapsed_widgets = []
        self.v_url = ctk.StringVar()
        self.v_user = ctk.StringVar()
        self.v_pwd = ctk.StringVar()
        self.v_hook = ctk.StringVar()
        self.v_lat = ctk.StringVar()
        self.v_lng = ctk.StringVar()
        self.v_auto = ctk.BooleanVar(value=True)
        self.v_sem = ctk.BooleanVar(value=False)
        self.v_live = ctk.BooleanVar(value=True)
        self.v_follow = ctk.BooleanVar(value=True)
        self.v_live_course = ctk.StringVar(value="登录后读取当前直播课")

        self._body()
        self._load_fields()
        self._loading = False
        self._saved_values = self._field_values()
        for var in (self.v_url, self.v_user, self.v_pwd, self.v_hook,
                    self.v_lat, self.v_lng, self.v_auto, self.v_sem, self.v_live):
            var.trace_add("write", self._mark_dirty)
        self.v_live.trace_add("write", self._update_audio_mode)
        self.v_url.trace_add("write", self._url_edited)
        self.v_user.trace_add("write", self._account_edited)
        self.v_pwd.trace_add("write", self._account_edited)
        self.txt_rules.bind("<<Modified>>", self._rules_changed, add="+")
        self.txt_rules.edit_modified(False)
        self.bind("<Control-s>", lambda _: self.save_cfg())
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll_job = self.after(100, self._poll_log)
        self._tick_job = self.after(1000, self._tick)
        self._courses_job = self.after(350, self._load_saved_live_courses)

    @staticmethod
    def _label(parent, text, size=12, color=design.TXT2, bold=False, family=None, **kw):
        kw.setdefault("height", 0)
        kw.setdefault("anchor", "w")
        family = family or design.F
        if bold and family == design.F and design.FH != design.F:
            family, bold = design.FH, False
        return ctk.CTkLabel(
            parent, text=text, font=(family, size, "bold") if bold else (family, size),
            text_color=color, **kw)

    @staticmethod
    def _divider(parent, pady=16):
        ctk.CTkFrame(parent, height=1, fg_color=design.RAIL,
                     corner_radius=0).pack(fill="x", pady=pady)

    @staticmethod
    def _fit_wrap(label, pixels, minimum, padding):
        width = max(minimum, int(pixels / label._get_widget_scaling()) - padding)
        if label.cget("wraplength") != width:
            label.configure(wraplength=width)

    @staticmethod
    def _fit_ellipsis(label, text, pixels):
        """用实际字体像素宽度省略，避免中文与 DPI 缩放导致右侧裁切。"""
        font_name = str(label._label.cget("font"))
        key = (text, pixels, font_name)
        if getattr(label, "_ellipsis_key", None) == key:
            return
        label._ellipsis_key = key
        if getattr(label, "_measure_font_name", None) != font_name:
            label._measure_font_name = font_name
            label._measure_font = tkfont.Font(font=font_name)
        font = label._measure_font
        available = max(0, pixels)
        fitted = text
        if font.measure(text) > available:
            low, high = 0, len(text)
            while low < high:
                middle = (low + high + 1) // 2
                if font.measure(text[:middle] + "…") <= available:
                    low = middle
                else:
                    high = middle - 1
            fitted = text[:low] + "…" if font.measure("…") <= available else ""
        if label.cget("text") != fitted:
            label.configure(text=fitted)

    @staticmethod
    def _button(parent, text, command, **kw):
        kw.setdefault("height", 34)
        kw.setdefault("corner_radius", design.RADIUS)
        kw.setdefault("font", design.BTN_FONT)
        kw.setdefault("fg_color", "transparent")
        kw.setdefault("hover_color", design.GHOST_HOVER)
        kw.setdefault("text_color", design.TXT2)
        kw.setdefault("text_color_disabled", design.TXT3)
        return ctk.CTkButton(parent, text=text, command=command, **kw)

    def _icon(self, name, size=16, color=design.TXT2):
        # 图片绑定当前窗口的 Tcl 生命周期，不跨 App 共享 PhotoImage。
        icons = self.__dict__.setdefault("_icons", {})
        key = (name, size, tuple(color) if isinstance(color, list) else color)
        if key not in icons:
            icons[key] = design.ui_icon(name, size, color)
        return icons[key]


    def _body(self):
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True)
        body.grid_columnconfigure(0, weight=0)
        body.grid_columnconfigure(1, weight=0)
        body.grid_columnconfigure(2, weight=1)
        body.grid_rowconfigure(0, weight=1)
        body.grid_rowconfigure(1, weight=0)  # 底部按钮行

        # 侧栏内容（可滚动）。
        self._sidebar_content = ctk.CTkFrame(
            body, width=SIDEBAR_WIDTH, fg_color=design.CARD, corner_radius=0)
        self._sidebar_content.grid(row=0, column=0, sticky="nsew")
        self._sidebar_content.grid_propagate(False)
        self._sidebar_content.pack_propagate(False)
        self._build_sidebar_header()
        self._build_sidebar_content()

        # 底部操作按钮：固定在侧栏下方。
        self._build_sidebar_actions(body)
        self._select_tab("basic")

        # 侧栏与主区之间的发丝中缝。
        ctk.CTkFrame(body, width=1, fg_color=design.RAIL,
                     corner_radius=0).grid(row=0, column=1, rowspan=2, sticky="ns")
        self._monitor_panel(body)

    def _build_sidebar_header(self):
        header = ctk.CTkFrame(self._sidebar_content, fg_color="transparent")
        header.pack(fill="x", padx=8, pady=(10, 4))
        self.b_collapse = self._button(
            header, "", self._toggle_sidebar, width=26, height=26, corner_radius=4,
            image=self._icon("collapse", 12, design.TXT3))
        self.b_collapse.pack(side="right")

        # Notion 式工作区行：头像 + 账号 + 下拉菜单。
        account = self._account_row = ctk.CTkFrame(
            self._sidebar_content, fg_color="transparent", corner_radius=4, height=30)
        account.pack(fill="x", padx=6, pady=(0, 2))
        account.pack_propagate(False)
        account.configure(cursor="hand2")
        self._account_inner = ctk.CTkFrame(account, fg_color="transparent")
        self._account_inner.pack(fill="both", expand=True, padx=4, pady=3)
        self._account_avatar = ctk.CTkLabel(
            self._account_inner, text="·", width=20, height=20, corner_radius=4,
            fg_color=design.GHOST_HOVER, text_color=design.TXT2,
            font=(design.F, 10, "bold"))
        self._account_avatar.pack(side="left")
        self._account_state = self._label(self._account_inner, "未登录", 13, design.TXT2)
        self._account_state.pack(side="left", padx=(7, 0))
        self._account_caret = ctk.CTkLabel(
            self._account_inner, text="", image=self._icon("chevron", 10, design.TXT3), width=14)
        self._account_caret.pack(side="right")
        self._bind_click(account, lambda _: self._open_account_menu())
        account.bind("<Enter>", lambda _: account.configure(fg_color=design.GHOST_HOVER))
        account.bind("<Leave>", lambda _: account.configure(fg_color="transparent"))

        # Notion 式侧栏导航。
        nav = self._nav_frame = ctk.CTkFrame(self._sidebar_content, fg_color="transparent")
        nav.pack(fill="x", padx=6, pady=(2, 6))
        for key, title, icon in (("basic", "直播", "camera"), ("extras", "设置", "sliders")):
            tab = ctk.CTkButton(
                nav, text=title, command=lambda k=key: self._select_tab(k),
                height=32, anchor="w", corner_radius=4, font=(design.F, 13),
                fg_color="transparent", hover_color=design.GHOST_HOVER,
                text_color=design.TXT2, text_color_disabled=design.TXT3,
                image=self._icon(icon, 14, design.TXT2), compound="left")
            tab.pack(fill="x", pady=(0, 1))
            tab._icon = icon
            tab._title = title
            self._tabs[key] = tab

    def _toggle_sidebar(self):
        if self._account_menu is not None and self._account_menu.winfo_exists():
            self._account_menu.destroy()
            self._account_menu = None
        self._sidebar_collapsed = not self._sidebar_collapsed
        collapsed = self._sidebar_collapsed
        width = SIDEBAR_COLLAPSED if collapsed else SIDEBAR_WIDTH
        self._sidebar_content.configure(width=width)
        self._sidebar_actions.configure(width=width)
        self.b_collapse.configure(
            image=self._icon("expand" if collapsed else "collapse", 12, design.TXT3))
        # 折叠时隐藏文字与多控件区，只保留图标导航和主按钮。
        for frame in self._collapsed_widgets:
            if collapsed:
                frame.pack_forget()
            else:
                frame.pack(side="left", padx=4)
        if collapsed:
            self._account_state.pack_forget()
            self._account_caret.pack_forget()
            self._content_frame.pack_forget()
            self._save_state.pack_forget()
        else:
            self._account_state.pack(side="left", padx=(7, 0))
            self._account_caret.pack(side="right")
            self._content_frame.pack(fill="both", expand=True, padx=(16, 4))
            self._update_actions_layout()
        for tab in self._tabs.values():
            tab.configure(text="" if collapsed else tab._title)
        self.b_monitor.configure(
            text="" if collapsed else self._monitor_button_text(),
            width=36 if collapsed else 0)

    def _monitor_button_text(self):
        if self._task_kind == "monitor":
            return "停止监控"
        if self._scheduled_course is not None:
            return "取消预约"
        selected = self._live_course_lookup.get(self.v_live_course.get())
        if selected is not None and selected.start > time.time():
            return "预约直播"
        return "开始监控"

    def _build_sidebar_content(self):
        content = self._content_frame = ctk.CTkFrame(self._sidebar_content, fg_color="transparent")
        content.pack(fill="both", expand=True, padx=(16, 4))
        content.grid_rowconfigure(0, weight=1)
        content.grid_columnconfigure(0, weight=1)
        for key in self._tabs:
            page = ScrollableFrame(
                content, fg_color=design.CARD, corner_radius=0,
                scrollbar_fg_color=design.CARD, scrollbar_button_color=design.CARD,
                scrollbar_button_hover_color=design.INPUT_BORDER)
            self._pages[key] = page
        # 填充两个页面的内容。
        self._basic_page(self._pages["basic"])
        settings_actions = ctk.CTkFrame(self._pages["extras"], fg_color="transparent")
        settings_actions.pack(fill="x", pady=(2, 14))
        self.b_save = self._button(
            settings_actions, "保存设置", self.save_cfg, width=88, height=30)
        self.b_save.pack(side="right")
        self.b_theme = self._button(
            settings_actions, "浅色" if self._appearance == "dark" else "深色",
            self.toggle_theme, width=72, height=30, font=(design.F, 12),
            image=self._icon("sun" if self._appearance == "dark" else "moon", 16),
            compound="left", corner_radius=4)
        self.b_theme.pack(side="left")
        self._account_page(self._pages["extras"])
        self._divider(self._pages["extras"], 16)
        self._advanced_page(self._pages["extras"])
        self._divider(self._pages["extras"], 16)
        self._signin_page(self._pages["extras"])
        self._divider(self._pages["extras"], 16)
        self._rules_page(self._pages["extras"])
        self._divider(self._pages["extras"], 16)
        self._extras_page(self._pages["extras"])

    def _build_sidebar_actions(self, parent):
        actions = self._sidebar_actions = ctk.CTkFrame(
            parent, width=SIDEBAR_WIDTH, height=96, fg_color=design.CARD, corner_radius=0)
        actions.grid(row=1, column=0, sticky="ew")
        actions.grid_propagate(False)
        actions.pack_propagate(False)
        self._save_state = self._label(
            actions, "", 11, design.TXT3, height=18)
        self.b_monitor = self._button(
            actions, "开始监控", self.toggle_monitor, height=36, corner_radius=4,
            fg_color=design.PRIMARY, hover_color=design.PRIMARY_HOVER,
            text_color=design.BUTTON_TEXT, text_color_disabled=design.BUTTON_DISABLED,
            image=self._icon("play", 14, design.BUTTON_TEXT), compound="left",
            font=(design.F, 13))
        self.b_monitor.pack(fill="x", padx=14)
        self.b_monitor.bind("<Enter>", lambda _: self._monitor_hover(True), add="+")
        self.b_monitor.bind("<Leave>", lambda _: self._monitor_hover(False), add="+")
        self._update_actions_layout()

    def _monitor_hover(self, entered):
        if self._task_kind == "monitor" or self._scheduled_course is not None:
            color = design.RED_HOVER if entered else design.RED
            self.b_monitor.configure(
                text_color=color,
                image=self._icon("stop" if self._task_kind == "monitor" else "clock", 15, color))

    def _update_actions_layout(self):
        """根据保存状态提示是否可见，调整底部按钮区的布局。"""
        show_hint = bool(self._save_state.cget("text"))
        # 只重新排列，不 hide 按钮。
        self._save_state.pack_forget()
        if show_hint and not self._sidebar_collapsed:
            self._save_state.pack(fill="x", padx=14, pady=(12, 8), before=self.b_monitor)
            self.b_monitor.pack_configure(pady=(0, 20))
        else:
            self.b_monitor.pack_configure(pady=(26, 24))


    def _field(self, parent, key, title, var, show="", action=None, bottom=16):
        box = ctk.CTkFrame(parent, fg_color="transparent")
        box.pack(fill="x", pady=(0, bottom))
        caption = ctk.CTkFrame(box, fg_color="transparent", height=22)
        caption.pack(fill="x", pady=(0, 6))
        caption.pack_propagate(False)
        label = self._label(caption, title, 12, design.TXT2)
        label.pack(side="left")
        self._field_labels[key] = label
        if action:
            button = self._button(
                caption, action[0], action[1], width=60, height=22,
                font=(design.F, 12), text_color=design.TXT2)
            button.pack(side="right")
            self._field_actions[key] = button
        entry = Entry(box, textvariable=var, show=show)
        entry.pack(fill="x")
        self._entries[key] = entry
        return entry

    def _basic_page(self, page):
        course_actions = ctk.CTkFrame(page, fg_color="transparent")
        course_actions.pack(fill="x", pady=(4, 6))
        self._label(course_actions, "今明直播课", 12, design.TXT2).pack(side="left")
        self.b_course_refresh = self._button(
            course_actions, "刷新", self.refresh_live_courses, width=44, height=22,
            font=(design.F, 11), text_color=design.TXT3)
        self.b_course_refresh.pack(side="right")
        self._course_list = ctk.CTkFrame(page, fg_color="transparent")
        self._course_list.pack(fill="x")
        self._course_cards = {}
        self._render_course_list()
        self._live_course_hint = self._label(
            page, "登录后选择课程，自动判断预约或监控。",
            11, design.TXT3, wraplength=470, justify="left")
        self._live_course_hint.pack(fill="x", pady=(8, 10))
        page.bind("<Configure>", lambda e: self._fit_wrap(self._live_course_hint, e.width, 100, 8), add="+")

    def _open_account_menu(self):
        if self._account_menu is not None and self._account_menu.winfo_exists():
            self._account_menu.destroy()
            self._account_menu = None
            return
        user = self.v_user.get().strip()
        items = []
        if user:
            items.append((f"切换账号（{user}）", self._menu_switch_account))
        else:
            items.append(("登录直播账号", self._menu_login))
        items.append("-")
        items.append(("打开账号设置", lambda: self._select_tab("extras")))
        menu = NotionMenu(self, items)
        self._account_menu = menu
        menu.open_for(self._account_row)

    def _menu_login(self):
        self._select_tab("extras")
        self._set_account_form(True)
        self._entries["user"].focus_set()

    def _menu_switch_account(self):
        self._set_account_form(True)
        self.do_live_login(switch_account=True)

    def _account_page(self, page):
        self._label(page, "直播账号", 13, design.TXT, True).pack(anchor="w", pady=(2, 8))
        self._account_form = ctk.CTkFrame(page, fg_color="transparent")
        inner = ctk.CTkFrame(self._account_form, fg_color="transparent")
        inner.pack(fill="x", pady=(0, 10))
        self._field(inner, "user", "账号", self.v_user, bottom=8)
        self.e_pwd = self._field(
            inner, "pwd", "密码", self.v_pwd, show="•",
            action=("显示密码", self._toggle_password), bottom=10)
        self.b_pwd = self._field_actions["pwd"]
        self.e_pwd.bind("<Return>", lambda _: self.do_live_login())
        self.b_live_login = self._button(
            inner, "登录并读取课程", self.do_live_login, height=36,
            fg_color=design.PRIMARY, hover_color=design.PRIMARY_HOVER,
            text_color=design.BUTTON_TEXT, corner_radius=design.RADIUS,
            font=(design.F, 13, "bold"))
        self.b_live_login.pack(fill="x")
        self._set_account_form(True)

    def _toggle_account_form(self):
        self._set_account_form(not self._account_expanded)

    def _set_account_form(self, expanded):
        self._account_expanded = expanded
        if expanded:
            self._account_form.pack(fill="x")
        else:
            self._account_form.pack_forget()
        self._refresh_account_summary()

    def _refresh_account_summary(self):
        user = self.v_user.get().strip()
        self._account_avatar.configure(text=user[:1] if user else "·")
        if not user:
            self._account_state.configure(text="未登录", text_color=design.TXT3)
        elif self._needs_live_login:
            self._account_state.configure(text=f"{user} · 待重新登录", text_color=design.AMBER)
        else:
            self._account_state.configure(text=user, text_color=design.TXT)

    @staticmethod
    def _bind_click(widget, command):
        widget.bind("<Button-1>", command)
        for child in widget.winfo_children():
            App._bind_click(child, command)

    def _render_course_list(self):
        for child in self._course_list.winfo_children():
            child.destroy()
        self._course_cards = {}
        if not self._live_course_lookup:
            self._label(self._course_list, self.v_live_course.get(), 12, design.TXT3
                        ).pack(padx=2, pady=8, anchor="w")
            return
        for label, course in self._live_course_lookup.items():
            card = self._build_course_card(self._course_list, label, course)
            card.pack(fill="x", pady=(0, 6))
            self._course_cards[label] = (card, course)
        self._refresh_course_cards()

    def _build_course_card(self, parent, label, course):
        # Notion 侧栏式扁平行：无盒子，选中/悬停只换底色。
        card = ctk.CTkFrame(parent, fg_color="transparent", corner_radius=5)
        card.configure(cursor="hand2")
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="x", padx=10, pady=10)
        top = ctk.CTkFrame(inner, fg_color="transparent")
        top.pack(fill="x")
        zone = dt.timezone(dt.timedelta(hours=8))
        start = dt.datetime.fromtimestamp(course.start, zone)
        end = dt.datetime.fromtimestamp(course.end, zone)
        clock = self._label(top, f"{start:%H:%M}", 11, design.TXT3, family=design.FM)
        clock.pack(side="right", padx=(8, 0))
        title = self._label(top, course.title, 13, design.TXT, width=1)
        title.pack(side="left", fill="x", expand=True)
        title.bind("<Configure>", lambda e: self._fit_ellipsis(title, course.title, e.width), add="+")
        bottom = ctk.CTkFrame(inner, fg_color="transparent")
        bottom.pack(fill="x", pady=(2, 0))
        now = time.time()
        today = dt.datetime.now(zone).date()
        day = "今天" if start.date() == today else (
            "明天" if start.date() == today + dt.timedelta(days=1) else start.strftime("%m-%d"))
        if course.start <= now <= course.end:
            state, dot = "进行中", design.GREEN
        elif course.start > now:
            state, dot = "待开课", design.TXT3
        else:
            state, dot = "已结束", design.TXT3
        self._label(bottom, "●", 8, dot).pack(side="left", padx=(0, 5))
        summary = " · ".join(part for part in (state, day, course.teacher) if part)
        summary_label = self._label(bottom, summary, 12, design.TXT2, width=1)
        summary_label.pack(side="left", fill="x", expand=True)
        summary_label.bind("<Configure>", lambda e: self._fit_ellipsis(
            summary_label, summary, e.width), add="+")
        detail = " · ".join(part for part in (course.classroom, course.section) if part)
        if detail:
            detail_label = self._label(inner, detail, 12, design.TXT2, width=1)
            detail_label.pack(fill="x", padx=(12, 0), pady=(3, 0))
            detail_label.bind("<Configure>", lambda e: self._fit_ellipsis(
                detail_label, detail, e.width), add="+")
        self._bind_click(card, lambda _e, key=label: self._select_live_course(key))
        card.bind("<Enter>", lambda _e, key=label: self._course_hover(key, True))
        card.bind("<Leave>", lambda _e, key=label: self._course_hover(key, False))
        return card

    def _course_hover(self, label, entered):
        if entered and label != self.v_live_course.get():
            card = self._course_cards.get(label)
            if card:
                card[0].configure(fg_color=design.GHOST_HOVER)
        elif not entered:
            self._refresh_course_cards()

    def _refresh_course_cards(self):
        selected = self.v_live_course.get()
        for label, (card, course) in self._course_cards.items():
            if label == selected:
                fg = design.SELECTION
            elif (self._scheduled_course is not None
                    and self._urls_equal(course, self._scheduled_course)):
                fg = design.AMBER_SOFT
            else:
                fg = "transparent"
            card.configure(fg_color=fg)

    def _advanced_page(self, page):
        self._label(page, "声音来源", 13, design.TXT, True).pack(anchor="w", pady=(0, 8))
        self.b_manual_url = self._button(
            page, "手动填写网址 ›", self._toggle_manual_url, height=26,
            anchor="w", font=(design.F, 11))
        self.b_manual_url.pack(fill="x", pady=(0, 6))
        self._manual_url = ctk.CTkFrame(page, fg_color="transparent")
        self._field(self._manual_url, "url", "课程网址（选填）", self.v_url,
                    action=("打开 ↗", self.open_url), bottom=6)
        self.b_url = self._field_actions["url"]

        mode = self._audio_mode_row = ctk.CTkFrame(page, fg_color="transparent")
        mode.pack(fill="x", pady=(6, 5))
        switch = Switch(mode, self.v_live)
        switch.pack(side="right")
        self.b_live_account = self._button(
            page, "单独登录直播账号", lambda: self.do_live_login(switch_account=True),
            height=28, anchor="w", font=(design.F, 11), text_color=design.TXT2)
        label = self._label(mode, "后台音频", 12, design.TXT2)
        label.pack(side="left")
        label.bind("<Button-1>", switch.toggle)
        self._url_hint = self._label(
            page, "已在浏览器播放课程时可留空。",
            11, design.TXT3, wraplength=244, justify="left")
        self._url_hint.pack(fill="x", pady=(0, 4))
        self.b_live_account.pack(fill="x", pady=(4, 0))

    def _signin_page(self, page):
        self._switch_row(
            page, "自动签到", "使用首页账号，确认签到码后提交", self.v_auto)
        self.b_login = self._button(
            page, "重新登录签到", self.do_login, height=36,
            border_color=design.INPUT_BORDER, border_width=1)
        self.b_login.pack(fill="x")
        self._divider(page, (12, 8))
        self._location_fields(page)

    def _toggle_manual_url(self):
        self._show_manual_url(not bool(self._manual_url.winfo_manager()))

    def _show_manual_url(self, visible=True):
        if visible:
            self._manual_url.pack(fill="x", before=self._audio_mode_row)
        else:
            self._manual_url.pack_forget()
        self.b_manual_url.configure(text="收起手动网址 ‹" if visible else "手动填写网址 ›")

    def _rules_page(self, page):
        self._label(page, "签到关键词", 13, design.TXT, True).pack(anchor="w")
        self._label(page, "每行一项，支持 re: 正则表达式", 11, design.TXT3).pack(
            anchor="w", pady=(4, 12))
        self.txt_rules = Textbox(
            page, height=224, font=(design.F, 13), fg_color=design.INPUT_BG,
            text_color=design.TXT, border_color=design.INPUT_BORDER, border_width=1,
            corner_radius=design.RADIUS, wrap="word", spacing1=4, spacing3=4)
        self.txt_rules.pack(fill="x")
        self.txt_rules.bind("<FocusIn>", lambda _: self.txt_rules.configure(
            border_color=design.FOCUS), add="+")
        self.txt_rules.bind("<FocusOut>", lambda _: self.txt_rules.configure(
            border_color=design.INPUT_BORDER), add="+")
        self._divider(page, 16)
        self._switch_row(
            page, "语义辅助识别", "识别相近话术，在本机运行",
            self.v_sem)


    def _extras_page(self, page):
        self._field(
            page, "hook", "企业微信 Webhook", self.v_hook,
            show="•", action=("测试推送", self.test_webhook))
        self.b_test = self._field_actions["hook"]
        self._label(page, "选填，留空时仅在本机提醒", 11, design.TXT3).pack(anchor="w")

    def _location_fields(self, page):
        caption = ctk.CTkFrame(page, fg_color="transparent")
        caption.pack(fill="x", pady=(0, 8))
        self._label(caption, "签到位置", 13, design.TXT2).pack(side="left")
        self.b_locate = self._button(
            caption, "获取当前位置", self.locate, width=96, height=26,
            font=(design.F, 11))
        self.b_locate.pack(side="right")
        row = ctk.CTkFrame(page, fg_color="transparent")
        row.pack(fill="x")
        row.grid_columnconfigure((0, 1), weight=1, uniform="location")
        for i, (key, title, var) in enumerate((
                ("lat", "纬度", self.v_lat), ("lng", "经度", self.v_lng))):
            col = ctk.CTkFrame(row, fg_color="transparent")
            col.grid(row=0, column=i, sticky="ew", padx=(0, 10) if i == 0 else 0)
            self._field(col, key, title, var, bottom=8)
        self._location_hint = self._label(
            page, "可通过 Windows 定位填入，请核对后保存。",
            11, design.TXT3, wraplength=244, justify="left")
        self._location_hint.pack(fill="x")

    def _switch_row(self, parent, title, desc, variable):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=(0, 8))
        track = Switch(row, variable)
        track.pack(side="right", padx=(14, 2))
        text = ctk.CTkFrame(row, fg_color="transparent")
        text.pack(side="left", fill="x", expand=True)
        self._label(text, title, 13, design.TXT, True).pack(anchor="w")
        description = self._label(text, desc, 11, design.TXT3, justify="left", wraplength=260)
        description.pack(fill="x", pady=(3, 0))
        text.bind("<Configure>", lambda e: self._fit_wrap(description, e.width, 120, 4), add="+")
        for widget in (row, text, *text.winfo_children()):
            widget.bind("<Button-1>", track.toggle)
        return track

    def _select_tab(self, key):
        if self._active_tab == key:
            return
        if self._active_tab is not None:
            # CTk replays cached grid calls on DPI changes. Forget the hidden
            # page's placement so scaling cannot make it reappear on top.
            self._pages[self._active_tab].grid_forget()
            self._tabs[self._active_tab].configure(
                fg_color="transparent", text_color=design.TXT2, font=(design.F, 13))
            old = self._tabs[self._active_tab]
            old.configure(image=self._icon(old._icon, 14, design.TXT2))
        self._pages[key].grid(row=0, column=0, sticky="nsew")
        selected = self._tabs[key]
        selected.configure(
            fg_color=design.GHOST_HOVER, text_color=design.TXT,
            font=(design.FH, 13) if design.FH != design.F else (design.F, 13, "bold"))
        selected.configure(image=self._icon(selected._icon, 14, design.TXT))
        self._active_tab = key
        self._update_actions_layout()


    def _monitor_panel(self, body):
        panel = ctk.CTkFrame(body, fg_color=design.BG, corner_radius=0)
        panel.grid(row=0, column=2, rowspan=2, sticky="nsew")
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(1, weight=1)
        self._build_topbar(panel)
        # 整页滚动，正文最大宽度与窗口尺寸共同决定留白。
        canvas = self._page_scroll = ScrollableFrame(
            panel, fg_color=design.BG, corner_radius=0,
            scrollbar_fg_color=design.BG, scrollbar_button_color=design.BG,
            scrollbar_button_hover_color=design.INPUT_BORDER)
        canvas.grid(row=1, column=0, sticky="nsew")
        page = self._document = ctk.CTkFrame(canvas, fg_color="transparent")
        page.pack(fill="x", padx=48, pady=(28, 40))
        canvas.bind("<Configure>", self._resize_document, add="+")

        self._label(page, "", 28, design.TXT, width=36, height=36,
                    image=self._icon("radar", 36, design.TXT2),
                    anchor="center").pack(anchor="w", pady=(0, 8))
        self._label(page, "课堂监控", 30, design.TXT, True).pack(
            anchor="w", pady=(0, 20))
        self._hero = ctk.CTkFrame(page, fg_color="transparent")
        self._hero.pack(fill="x")

        properties = ctk.CTkFrame(self._hero, fg_color="transparent")
        properties.pack(fill="x", pady=(0, 26))
        status_row = ctk.CTkFrame(properties, fg_color="transparent")
        status_row.pack(fill="x", pady=(0, 8))
        self._label(status_row, "状态", 13, design.TXT2, width=124, height=26,
                    image=self._icon("status", 14, design.TXT3),
                    compound="left", padx=6).pack(side="left")
        self._status_chip = ctk.CTkFrame(
            status_row, fg_color=design.INSET, corner_radius=4)
        self._status_chip.pack(side="left")
        self._dot = self._label(self._status_chip, "●", 8, design.TXT3, height=24)
        self._dot.pack(side="left", padx=(8, 5))
        self._status = self._label(self._status_chip, "未开始", 12, design.TXT2, height=24)
        self._status.pack(side="left", padx=(0, 8))
        timer_row = ctk.CTkFrame(properties, fg_color="transparent")
        timer_row.pack(fill="x")
        self._timer_label = self._label(timer_row, "已运行时长", 13, design.TXT2,
                                        width=124, height=26,
                                        image=self._icon("clock", 14, design.TXT3),
                                        compound="left", padx=6)
        self._timer_label.pack(side="left")
        self._metrics = {}
        timer = self._label(timer_row, "00:00:00", 13, design.TXT2,
                            family=design.FM, width=90, height=24,
                            fg_color=design.INSET, corner_radius=4, anchor="center")
        timer.pack(side="left")
        self._metrics["time"] = timer

        self._label(self._hero, "实时转写内容", 12, design.TXT2).pack(
            anchor="w", pady=(0, 10))
        quote = ctk.CTkFrame(self._hero, fg_color="transparent")
        quote.pack(fill="x")
        quote.grid_columnconfigure(1, weight=1)
        ctk.CTkFrame(quote, width=3, height=1, fg_color=design.TXT, corner_radius=0).grid(
            row=0, column=0, sticky="ns")
        self._transcript = QuoteText(quote, "等待开始", text_color=design.TXT)
        self._transcript.grid(row=0, column=1, sticky="ew", padx=(14, 0), pady=4)
        quote.bind("<Configure>", lambda e: self._fit_wrap(
            self._transcript, e.width, 200, 17), add="+")
        self._transcript_hint = self._label(
            self._hero, "", 11, design.TXT3, wraplength=440, justify="left")
        self._transcript_hint.pack(fill="x", padx=(17, 0), pady=(8, 18))

        # Notion Callout 高亮块：淡黄底 + 左侧 Emoji，签到码识别后文字点亮。
        code_bar = self._code_bar = ctk.CTkFrame(
            self._hero, fg_color=design.CALLOUT, corner_radius=4,
            border_width=1, border_color=(design.CALLOUT[0], design.RAIL[1]))
        code_bar.pack(fill="x")
        inner = ctk.CTkFrame(code_bar, fg_color="transparent")
        inner.pack(fill="x", padx=16, pady=16)
        self._label(inner, "", image=self._icon("key", 18, design.CALLOUT_TEXT),
                    width=20).pack(side="left", padx=(0, 12))
        self._label(inner, "签到码", 12, design.CALLOUT_TEXT).pack(side="left", padx=(0, 16))
        self._digit_boxes = []
        digits = ctk.CTkFrame(inner, fg_color="transparent")
        digits.pack(side="left")
        for _ in range(4):
            tile = ctk.CTkFrame(digits, fg_color=design.SURFACE, corner_radius=4,
                                border_width=1, border_color=design.RAIL)
            tile.pack(side="left", padx=(0, 8))
            box = self._label(
                tile, "—", 24, design.TXT3, family=design.FM, bold=True,
                width=38, height=40, corner_radius=3, fg_color=design.SURFACE,
                anchor="center")
            box.pack(padx=1, pady=1)
            self._digit_boxes.append(box)
        self._metrics["code"] = self._digit_boxes[0]  # 供外部读取签到码展示状态
        self.b_copy = self._button(
            inner, "复制", self.copy_code, width=68, height=30, font=(design.F, 12),
            image=self._icon("copy", 13, design.TXT3), compound="left", state="disabled")
        self.b_copy.pack(side="left", padx=(18, 0))
        self._hero.bind("<Configure>", lambda e: self._fit_wrap(
            self._transcript_hint, e.width, 200, 17), add="+")

        self.b_details = self._button(
            page, "活动记录", self._toggle_log_details, height=32,
            image=self._icon("triangle-right", 12), compound="left",
            anchor="w", font=(design.F, 14), text_color=design.TXT2)
        self.b_details.pack(fill="x", pady=(22, 0))
        self._log_details = ctk.CTkFrame(page, fg_color="transparent")
        self._activity_list = ctk.CTkFrame(self._log_details, fg_color="transparent")
        self._activity_list.pack(fill="x", padx=(24, 0), pady=(8, 12))
        self._render_activity_records()
        head = ctk.CTkFrame(self._log_details, fg_color="transparent")
        head.pack(fill="x", pady=(4, 0))
        self._button(head, "清空", self.clear_log, width=42, height=26,
                     font=(design.F, 12)).pack(side="right")
        ctk.CTkCheckBox(
            head, text="自动滚动", variable=self.v_follow, width=90, height=22,
            checkbox_width=14, checkbox_height=14, corner_radius=4,
            border_width=1, border_color=design.TXT3, fg_color=design.PRIMARY,
            hover_color=design.PRIMARY_HOVER, checkmark_color=design.BUTTON_TEXT,
            text_color=design.TXT3, font=(design.F, 12)).pack(side="right", padx=(0, 8))
        self.log = Textbox(
            self._log_details, font=(design.F, 13), height=180, fg_color=design.BG,
            text_color=design.TXT2, border_width=0, corner_radius=0,
            wrap="word", spacing1=6, spacing3=6,
            scrollbar_button_color=design.BG,
            scrollbar_button_hover_color=design.INPUT_BORDER)
        self.log.pack(fill="both", expand=True, pady=(4, 8))
        self._apply_log_colors()
        self.log.configure(state="disabled")
        self._empty_log = self._label(self.log, "监控开始后，活动记录会显示在这里", 12, design.TXT3)
        self._empty_log.place(relx=0.5, rely=0.5, anchor="center")

    def _toggle_log_details(self):
        self._set_log_details(not bool(self._log_details.winfo_manager()))

    def _set_log_details(self, visible):
        if visible:
            self._render_activity_records(force=True)
            self._log_details.pack(fill="x", pady=(0, 12))
        else:
            self._log_details.pack_forget()
        self.b_details.configure(image=self._icon(
            "triangle-down" if visible else "triangle-right", 12))

    def _resize_document(self, event):
        width = event.width / self._page_scroll._get_widget_scaling()
        padding = max(48, (width - CANVAS_MAX_WIDTH) / 2)
        if self._document_padding != padding:
            self._document_padding = padding
            self._document.pack_configure(padx=padding)

    def _build_topbar(self, panel):
        bar = self._topbar = ctk.CTkFrame(panel, height=40, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew")
        bar.pack_propagate(False)
        self._button(bar, "HDUSign", lambda: self._select_tab("basic"),
                     width=78, height=26, font=(design.FL, 12)).pack(side="left", padx=(12, 0))
        self._label(bar, "/", 12, design.TXT3).pack(side="left", padx=4)
        self._button(bar, "课堂监控", self._scroll_to_top,
                     image=self._icon("radar", 15), compound="left",
                     width=108, height=28, font=(design.F, 13)).pack(side="left")
        self.b_more = self._button(bar, "", self._open_page_menu, width=28, height=28,
                                   image=self._icon("more", 16))
        self.b_more.pack(side="right", padx=(2, 12))
        self.b_activity = self._button(bar, "", self._open_activity, width=28, height=28,
                                       image=self._icon("history", 16))
        self.b_activity.pack(side="right", padx=2)
        self.b_focus = self._button(bar, "", self._toggle_sidebar, width=28, height=28,
                                    image=self._icon("focus", 16))
        self.b_focus.pack(side="right", padx=2)

    def _scroll_to_top(self):
        self._page_scroll._parent_canvas.yview_moveto(0)

    def _open_activity(self):
        self._set_log_details(True)
        self.update_idletasks()
        canvas = self._page_scroll._parent_canvas
        region = canvas.bbox("all")
        if region:
            canvas.yview_moveto(self.b_details.winfo_y() / max(1, region[3]))

    def _open_page_menu(self):
        if self._page_menu is not None and self._page_menu.winfo_exists():
            self._page_menu.destroy()
            self._page_menu = None
            return
        self._page_menu = NotionMenu(self, [
            ("退出聚焦" if self._sidebar_collapsed else "聚焦页面", self._toggle_sidebar),
            ("切换浅色外观" if self._appearance == "dark" else "切换深色外观", self.toggle_theme),
            ("打开设置", self._open_settings),
        ])
        self._page_menu.open_for(self.b_more)

    def _open_settings(self):
        if self._sidebar_collapsed:
            self._toggle_sidebar()
        self._select_tab("extras")

    def _render_activity_records(self, force=False):
        self._activity_dirty = True
        if not force and not self._log_details.winfo_manager():
            return
        current_ids = {record["id"] for record in self._activity_records}
        for key in list(self._activity_cards):
            if key not in current_ids:
                self._activity_cards.pop(key).destroy()
        empty = getattr(self, "_activity_empty", None)
        if empty is not None:
            empty.pack_forget()
        if not self._activity_records:
            if empty is None:
                empty = self._activity_empty = self._label(
                    self._activity_list, "暂无签到记录", 13, design.TXT3)
            empty.pack(anchor="w", pady=8)
            self._activity_dirty = False
            return
        for record in self._activity_records:
            if record["id"] in self._activity_cards:
                continue
            card = ctk.CTkFrame(self._activity_list, fg_color="transparent",
                                corner_radius=4, border_width=1, border_color=design.RAIL)
            children = self._activity_list.pack_slaves()
            card.pack(fill="x", pady=(0, 8), **({"before": children[0]} if children else {}))
            self._activity_cards[record["id"]] = card
            self._label(card, record["time"], 11, design.TXT3,
                        family=design.FM).pack(anchor="w", padx=12, pady=(9, 4))
            label = self._label(card, record["text"], 13, design.TXT,
                                wraplength=480, justify="left")
            label.pack(fill="x", padx=12, pady=(0, 9))
            card.bind("<Configure>", lambda e, target=label: self._fit_wrap(
                target, e.width, 160, 26), add="+")
        self._activity_dirty = False

    def _record_activity(self, line):
        text = re.sub(r"^\[(?:i|OK|!|错误|warn)\]\s*", "", line)
        self._activity_sequence += 1
        self._activity_records.append({"id": self._activity_sequence,
                                       "time": time.strftime("%m-%d %H:%M:%S"), "text": text})
        del self._activity_records[:-20]

    # ---------- 配置与即时反馈 ----------

    def _load_fields(self):
        self.v_url.set(str(self.cfg.get("live_url", "") or ""))
        self.v_live.set(bool((self.cfg.get("live_audio") or {}).get("enabled", True)))
        self.v_user.set(self.secrets.get("skl_username", ""))
        self.v_pwd.set(self.secrets.get("skl_password", ""))
        hook = ((self.cfg.get("alert") or {}).get("webhook") or {}).get("url", "")
        self.v_hook.set(str(hook or ""))
        location = self.cfg.get("location") or {}
        self.v_lat.set(str(location.get("lat", DEFAULT_LAT)))
        self.v_lng.set(str(location.get("lng", DEFAULT_LNG)))
        self.v_auto.set(bool((self.cfg.get("auto_sign") or {}).get("enabled", True)))
        rules = self.cfg.get("rules") or {}
        self.v_sem.set(bool((rules.get("semantic") or {}).get("enabled", False)))
        self.txt_rules.insert("1.0", "\n".join(str(r) for r in rules.get("strong", [])))
        self._update_audio_mode()
        self._restore_scheduled_course()
        self._refresh_account_summary()

    def _field_values(self):
        return (
            self.v_url.get(), self.v_user.get(), self.v_pwd.get(),
            self.v_hook.get(), self.v_lat.get(), self.v_lng.get(),
            self.v_auto.get(), self.v_sem.get(),
            self.txt_rules.get("1.0", "end-1c"),
            self.v_live.get(),
        )

    @staticmethod
    def _course_label(course, *, now=None):
        title = course.title
        title = title if len(title) <= 12 else title[:11] + "…"
        zone = dt.timezone(dt.timedelta(hours=8))
        course_when = dt.datetime.fromtimestamp(course.start, zone)
        current = (dt.datetime.fromtimestamp(now, zone) if now is not None
                   else dt.datetime.now(zone))
        tomorrow = current.date() + dt.timedelta(days=1)
        if course_when.date() == tomorrow:
            when = "明日 " + course_when.strftime("%H:%M")
        elif course_when.date() > tomorrow:
            when = course_when.strftime("%m-%d %H:%M")
        else:
            when = course_when.strftime("%H:%M")
        detail = course.teacher or course.classroom
        detail = detail if len(detail) <= 8 else detail[:7] + "…"
        if course_when.date() > current.date():
            return " · ".join(part for part in (when, title, detail) if part)
        suffix = " · ".join(part for part in (when, detail) if part)
        return f"{title} · {suffix}" if suffix else title

    @staticmethod
    def _scheduled_course_data(course: LiveCourse) -> dict:
        return {
            "course_id": course.course_id,
            "title": course.title,
            "start": course.start,
            "end": course.end,
            "teacher": course.teacher,
            "classroom": course.classroom,
            "section": course.section,
            "tecl_id": course.tecl_id,
            "origin": course.origin,
        }

    @staticmethod
    def _scheduled_course_from_data(value: object) -> LiveCourse | None:
        if not isinstance(value, dict):
            return None
        try:
            course = LiveCourse(
                str(value["course_id"]), str(value["title"]),
                float(value["start"]), float(value["end"]),
                str(value.get("teacher", "")), str(value.get("classroom", "")),
                str(value.get("section", "")), str(value.get("tecl_id", "")),
                str(value.get("origin", "https://course.hdu.edu.cn")),
            )
        except (KeyError, TypeError, ValueError):
            return None
        if not (math.isfinite(course.start) and math.isfinite(course.end)
                and course.end > course.start):
            return None
        return course

    def _restore_scheduled_course(self):
        stored = self.cfg.get("scheduled_course")
        course = self._scheduled_course_from_data(stored)
        now = time.time()
        if course is None or course.end <= now:
            if stored is not None:
                self.cfg.pop("scheduled_course", None)
                self._write_scheduled_course(None)
            if stored is not None:
                self._live_course_hint.configure(
                    text="已忽略无效或已结束的预约。", text_color=design.AMBER)
            return
        self._scheduled_course = course
        from hdusign.live import live_course_url

        try:
            self.v_url.set(live_course_url(course))
            self.v_live.set(True)
            self._update_audio_mode()
        except Exception:
            self._scheduled_course = None
            self.cfg.pop("scheduled_course", None)
            self._write_scheduled_course(None)
            return
        self._show_scheduled_state(restored=True)

    def _set_live_course_choices(self, courses):
        self._live_courses = list(courses or [])
        self._live_course_lookup = {}
        labels = []
        for index, course in enumerate(self._live_courses):
            base = self._course_label(course)
            label = base if base not in self._live_course_lookup else f"{base} ({index + 1})"
            self._live_course_lookup[label] = course
            labels.append(label)
        if labels:
            from hdusign.live import live_course_url

            current = "请选择直播课程"
            for label, course in self._live_course_lookup.items():
                try:
                    if live_course_url(course) == self.v_url.get().strip():
                        current = label
                        break
                except Exception:
                    continue
            self.v_live_course.set(current)
            self._live_course_hint.configure(
                text=f"找到 {len(labels)} 节今明直播课，选择后可开始监控或预约。",
                text_color=design.TXT3)
            if self._scheduled_course is not None:
                self._show_scheduled_state(restored=True)
        else:
            text = "当前没有可选择的直播课"
            self.v_live_course.set(text)
            self._live_course_hint.configure(
                text="今明两天没有可选择的直播课，可稍后刷新或在设置中手动粘贴链接。",
                text_color=design.AMBER)
            if self._scheduled_course is not None:
                self._show_scheduled_state(restored=True)
        self._render_course_list()
        self._update_monitor_button()

    def _select_live_course(self, label):
        if self._task_kind or self._closing:
            return
        course = self._live_course_lookup.get(label)
        if course is None:
            return
        from hdusign.live import live_course_url

        if self._scheduled_course is not None:
            self._cancel_scheduled_course(notify=False)

        self.v_live_course.set(label)
        self.v_url.set(live_course_url(course))
        self.v_live.set(True)
        self._entries["url"].invalid = False
        self._entries["url"].configure(border_color=design.INPUT_BORDER)
        zone = dt.timezone(dt.timedelta(hours=8))
        when = dt.datetime.fromtimestamp(course.start, zone).strftime("%m-%d %H:%M")
        end = dt.datetime.fromtimestamp(course.end, zone).strftime("%H:%M")
        details = " · ".join(part for part in (course.classroom, course.section) if part)
        self._live_course_hint.configure(
            text=f"{when}–{end}  {details}".strip(),
            text_color=design.TXT2)
        self._refresh_course_cards()
        self._update_monitor_button()

    def _load_saved_live_courses(self):
        self._courses_job = None
        if not self._closing and self._task_kind is None and (CONFIG.parent / "live_session.json").is_file():
            try:
                saved = json.loads((CONFIG.parent / "live_session.json").read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                saved = {}
            username = saved.get("username") if isinstance(saved, dict) else None
            if username is not None and username != self.v_user.get().strip():
                self._account_edited()
                return
            self.refresh_live_courses()

    def _account_edited(self, *_):
        if self._loading or self._task_kind is not None:
            return
        self._needs_live_login = True
        self._live_courses = []
        self._live_course_lookup = {}
        if self.v_live.get():
            self.v_url.set("")
        self.v_live_course.set("请登录并读取课程")
        self._render_course_list()
        self._live_course_hint.configure(text="账号信息已修改，请点击「登录并读取课程」。",
                                         text_color=design.TXT3)
        self._set_account_form(True)
        self._update_monitor_button()

    def _url_edited(self, *_):
        from hdusign.live import live_course_url
        if self._scheduled_course is not None:
            try:
                scheduled_url = live_course_url(self._scheduled_course)
            except Exception:
                scheduled_url = ""
            if self.v_url.get().strip() != scheduled_url:
                self._cancel_scheduled_course(notify=True)
        selected = self._live_course_lookup.get(self.v_live_course.get())
        if selected is not None and live_course_url(selected) != self.v_url.get().strip():
            self.v_live_course.set("请选择直播课程")
            self._live_course_hint.configure(text="已改为手动地址，或重新选择列表中的课程。",
                                             text_color=design.TXT3)
        self._refresh_course_cards()

    def refresh_live_courses(self):
        if self._busy():
            return
        if self._needs_live_login:
            self._feedback("请先点击「登录并读取课程」确认当前账号", error=True)
            return
        self._live_course_hint.configure(text="正在读取直播课程…", text_color=design.TXT3)
        self._live_course_lookup = {}
        self.v_live_course.set("正在读取直播课程…")
        self._render_course_list()
        self.stop_event.clear()

        def run(cfg):
            from hdusign.live import list_live_courses

            return list_live_courses(cfg, stop=self.stop_event)

        cfg = copy.deepcopy(self.cfg)
        cfg["live_url"] = self.v_url.get().strip()
        self._worker(run, cfg, kind="live_courses")

    @staticmethod
    def _course_time_text(course: LiveCourse, *, now=None) -> str:
        zone = dt.timezone(dt.timedelta(hours=8))
        course_when = dt.datetime.fromtimestamp(course.start, zone)
        current = (dt.datetime.fromtimestamp(now, zone) if now is not None
                   else dt.datetime.now(zone))
        if course_when.date() == current.date():
            prefix = "今天"
        elif course_when.date() == current.date() + dt.timedelta(days=1):
            prefix = "明日"
        else:
            prefix = course_when.strftime("%m-%d")
        return f"{prefix} {course_when.strftime('%H:%M')}"

    def _update_monitor_button(self):
        idle = not self._closing and self._task_kind is None
        selected = self._live_course_lookup.get(self.v_live_course.get())
        future = selected is not None and selected.start > time.time()
        collapsed = self.__dict__.get("_sidebar_collapsed", False)
        destructive = self._task_kind == "monitor" or self._scheduled_course is not None
        signature = (idle, future, collapsed, destructive, self._task_kind,
                     self._scheduled_course is not None)
        if self.__dict__.get("_monitor_button_signature") == signature:
            return
        self._monitor_button_signature = signature
        self.b_monitor.configure(
            fg_color="transparent" if destructive else design.PRIMARY,
            hover_color=design.RED_SOFT if destructive else design.PRIMARY_HOVER,
            text_color=design.RED if destructive else design.BUTTON_TEXT,
            text_color_disabled=design.TXT3 if destructive else design.BUTTON_DISABLED,
            border_width=1 if destructive else 0, border_color=design.RED_BORDER)
        if self._task_kind == "monitor":
            self.b_monitor.configure(
                text="" if collapsed else "停止监控",
                image=self._icon("stop", 15, design.RED))
            return
        title = ("取消预约" if self._scheduled_course is not None else
                 "预约直播" if future else "开始监控")
        self.b_monitor.configure(
            text="" if collapsed else title,
            image=self._icon("clock" if future or self._scheduled_course else "play",
                                 15, design.RED if destructive else design.BUTTON_TEXT),
            state="normal" if idle else "disabled")

    def _write_scheduled_course(self, course: LiveCourse | None) -> bool:
        cfg = load_cfg()
        if course is None:
            cfg.pop("scheduled_course", None)
        else:
            cfg["scheduled_course"] = self._scheduled_course_data(course)
        try:
            CONFIG.write_text(
                yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
        except OSError as exc:
            self._feedback(f"预约保存失败：{exc.strerror or '无法写入配置文件'}", error=True)
            return False
        self.cfg = cfg
        return True

    def _show_scheduled_state(self, restored=False):
        course = self._scheduled_course
        if course is None:
            return

        when = self._course_time_text(course)
        zone = dt.timezone(dt.timedelta(hours=8))
        end = dt.datetime.fromtimestamp(course.end, zone).strftime("%H:%M")
        details = " · ".join(part for part in (course.teacher, course.classroom, course.section) if part)
        self._update_monitor_button()
        self._set_status(design.AMBER, "已预约")
        self._live_course_hint.configure(
            text=(f"{course.title}\n{when}–{end}  {details}\n"
                  "到点会自动启动监控。"),
            text_color=design.TXT2)
        self._transcript.configure(text=f"等待{when}的课程开始", text_color=design.TXT2)
        self._transcript_hint.configure(
            text=("已恢复预约，到点自动启动监控。" if restored else
                  "预约已保存，请保持软件运行；点击「取消预约」可取消。"))
        label = next((label for label, candidate in self._live_course_lookup.items()
                      if self._urls_equal(candidate, course)), None)
        self.v_live_course.set(label or f"已预约 · {when}")
        self._refresh_course_cards()
        self._update_scheduled_countdown()

    @staticmethod
    def _urls_equal(left: LiveCourse, right: LiveCourse) -> bool:
        from hdusign.live import live_course_url

        try:
            return live_course_url(left) == live_course_url(right)
        except Exception:
            return False

    def _update_scheduled_countdown(self):
        if self._scheduled_course is None:
            return
        remaining = max(0, int(self._scheduled_course.start + SCHEDULE_START_DELAY - time.time()))
        days, seconds = divmod(remaining, 86400)
        clock = f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}"
        text = f"{days}天 {clock}" if days else clock
        self._metrics["time"].configure(text=text)

    def _schedule_monitor(self, course: LiveCourse) -> bool:
        if self._busy() or self._closing:
            return False
        now = time.time()
        if course.end <= now:
            self._live_course_hint.configure(
                text="这节课已经结束，无法预约。", text_color=design.AMBER)
            return False
        if course.start <= now:
            return self.start_monitor()
        self._scheduled_course = course
        if not self.save_cfg():
            self._scheduled_course = None
            self._update_monitor_button()
            return False
        self._show_scheduled_state()
        self.logline(f"[i] 已预约监控：{course.title} · {self._course_time_text(course)}")
        return True

    def _cancel_scheduled_course(self, notify=True) -> bool:
        course = self._scheduled_course
        if course is None:
            return False
        self._scheduled_course = None
        persisted = self._write_scheduled_course(None)
        self._update_monitor_button()
        if self._task_kind is None:
            self._set_status(design.TXT3, "就绪")
        if notify:
            when = self._course_time_text(course)
            self._live_course_hint.configure(
                text=f"已取消 {when} 的课程预约。", text_color=design.TXT3)
            self.logline(f"[i] 已取消监控预约：{course.title} · {when}")
        self._refresh_course_cards()
        return persisted

    def _discard_scheduled_course(self, reason: str):
        course = self._scheduled_course
        if course is None:
            return
        self._scheduled_course = None
        self._write_scheduled_course(None)
        self._update_monitor_button()
        if self._task_kind is None:
            self._set_status(design.TXT3, "就绪")
        self._live_course_hint.configure(text=reason, text_color=design.AMBER)
        self.logline(f"[!] {reason}")
        self._refresh_course_cards()

    def _check_scheduled_monitor(self):
        course = self._scheduled_course
        if course is None or self._closing:
            return
        now = time.time()
        if now < course.start + SCHEDULE_START_DELAY:
            self._update_scheduled_countdown()
            return
        if now >= course.end:
            self._discard_scheduled_course("预约课程已结束，未启动监控。")
            return
        if self._task_kind is not None:
            return
        self._start_scheduled_monitor()

    def _start_scheduled_monitor(self):
        course = self._scheduled_course
        if course is None:
            return False
        self._scheduled_course = None
        self._update_monitor_button()
        self.logline(
            f"[i] 预约课程到点，正在启动监控：{course.title} · {self._course_time_text(course)}")
        started = self.start_monitor()
        if not started:
            self._scheduled_course = course
            self._show_scheduled_state()
        return started

    def _update_audio_mode(self, *_):
        direct = self.v_live.get()
        if (self._scheduled_course is not None and not direct and not self._loading):
            self._cancel_scheduled_course(notify=True)
        self._field_labels["url"].configure(
            text="手动直播网址" if direct else "课程网址（选填）")
        self.b_live_login.configure(
            text="登录中…" if self._task_kind == "live_login" else "登录并读取课程",
            state="disabled" if self._closing or self._task_kind else "normal")
        self.b_live_account.configure(
            state="disabled" if self._closing or self._task_kind else "normal")
        self._url_hint.configure(text=(
            "选课后自动开启，无需打开播放网页。" if direct
            else "电脑声音模式：请在浏览器播放课程。"))

    def _mark_dirty(self, *_):
        if not self._loading:
            dirty = self._field_values() != self._saved_values
            self._save_state.configure(
                text="有未保存的更改" if dirty else "已保存",
                text_color=design.AMBER if dirty else design.TXT3)
            self.b_save.configure(text_color=design.PRIMARY if dirty else design.TXT2)
            self._update_actions_layout()

    def _rules_changed(self, _=None):
        if self.txt_rules.edit_modified():
            self.txt_rules.edit_modified(False)
            self._mark_dirty()

    def _feedback(self, message, error=False):
        self._save_state.configure(text=message, text_color=design.RED if error else design.TXT3)
        if error or self._active_tab == "extras":
            pass  # 文本已设置，_update_actions_layout 会处理显示
        else:
            self._save_state.configure(text="")
        self._update_actions_layout()
        if error:
            self.logline(f"[!] {message}")

    def _field_error(self, key, message):
        self._select_tab("basic" if key == "url" else "extras")
        if key == "url":
            self._show_manual_url()
        self._entries[key].set_error()
        self._feedback(message, error=True)
        return False

    def save_cfg(self) -> bool:
        coords = {}
        for key, var, title, limit in (
                ("lat", self.v_lat, "纬度", 90),
                ("lng", self.v_lng, "经度", 180)):
            try:
                value = float(var.get().strip())
                if not math.isfinite(value) or not -limit <= value <= limit:
                    raise ValueError
            except ValueError:
                return self._field_error(key, f"{title}须为 {-limit} 到 {limit} 的数字")
            coords[key] = value
            self._entries[key].invalid = False
            self._entries[key].configure(border_color=design.INPUT_BORDER)

        cfg = copy.deepcopy(self.cfg)
        secrets = copy.deepcopy(self.secrets)
        cfg["ui"] = {**(cfg.get("ui") or {}), "appearance": self._appearance}
        cfg["live_url"] = self.v_url.get().strip()
        cfg.setdefault("live_audio", {})["enabled"] = bool(self.v_live.get())
        if self._scheduled_course is None:
            cfg.pop("scheduled_course", None)
        else:
            cfg["scheduled_course"] = self._scheduled_course_data(self._scheduled_course)
        cfg.setdefault("alert", {}).setdefault("webhook", {})["url"] = self.v_hook.get().strip()
        cfg["location"] = {**(cfg.get("location") or {}), **coords}
        cfg.setdefault("auto_sign", {})["enabled"] = bool(self.v_auto.get())
        cfg.setdefault("rules", {}).setdefault("semantic", {})["enabled"] = bool(self.v_sem.get())
        cfg["rules"]["strong"] = [
            line.strip() for line in self.txt_rules.get("1.0", "end").splitlines()
            if line.strip()]
        secrets["skl_username"] = self.v_user.get().strip()
        secrets["skl_password"] = self.v_pwd.get()
        try:
            CONFIG.write_text(
                yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
            SECRETS.write_text(
                json.dumps(secrets, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            self._feedback(f"保存失败：{exc.strerror or '无法写入配置文件'}", error=True)
            return False
        self.cfg, self.secrets = cfg, secrets
        self._saved_values = self._field_values()
        self.b_save.configure(text_color=design.TXT2)
        self._feedback("已保存 · 下次启动时生效" if self._task_kind else "设置已保存")
        return True

    def _theme_color(self, value):
        if isinstance(value, (tuple, list)):
            return value[0 if self._appearance == "light" else 1]
        return value

    def _apply_log_colors(self):
        # Tk 文本标签不接受 CTk 的双色元组，切换时同步已有日志。
        for tag, color in (("time", design.TXT3), ("info", design.TXT2), ("success", design.ACCENT),
                           ("error", design.RED), ("warn", design.AMBER), ("asr", design.TXT)):
            self.log.tag_config(tag, foreground=self._theme_color(color))

    def toggle_theme(self):
        self._appearance = "light" if self._appearance == "dark" else "dark"
        ctk.set_appearance_mode(self._appearance)
        self.b_theme.configure(
            text="浅色" if self._appearance == "dark" else "深色",
            image=self._icon("sun" if self._appearance == "dark" else "moon", 16))
        self._apply_log_colors()
        # 单独记住主题，不提交表单中尚未保存的账号或其他修改。
        cfg = copy.deepcopy(self.cfg)
        cfg["ui"] = {**(cfg.get("ui") or {}), "appearance": self._appearance}
        try:
            CONFIG.write_text(
                yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
        except OSError:
            self._feedback("主题已切换，但偏好保存失败", error=True)
            return
        self.cfg = cfg

    def _toggle_password(self):
        hidden = bool(self.e_pwd.cget("show"))
        self.e_pwd.configure(show="" if hidden else "•")
        self.b_pwd.configure(text="隐藏密码" if hidden else "显示密码")

    # ---------- 后台动作与主线程状态 ----------
    def _busy(self):
        if self._closing:
            return True
        if self._task_kind is not None:
            self.logline("[!] 已有任务在运行，请等待完成或停止监控。")
            return True
        return False

    def _worker(self, fn, *args, kind="monitor"):
        if self._busy():
            return False
        self._task_kind = kind
        self._monitor_button_signature = None
        self.b_monitor.configure(state="disabled")
        for key in ("user", "pwd"):
            self._entries[key].configure(state="disabled")
        self.b_login.configure(state="disabled")
        self.b_test.configure(state="disabled")
        self.b_locate.configure(state="disabled")
        self.b_course_refresh.configure(state="disabled")
        self._update_audio_mode()
        if kind == "monitor":
            self._t0 = time.monotonic()
            self._metrics["time"].configure(text="00:00:00")
            self._set_code(None)
            self._has_transcript = False
            self._monitor_ready = False
            self._transcript.configure(text="正在准备语音识别…", text_color=design.TXT2)
            self._transcript_hint.configure(text="初始化本地模型，请稍候。")
            self._update_monitor_button()
            self.b_monitor.configure(state="normal")
            self._set_status(design.AMBER, "正在启动")
        elif kind == "login":
            self.b_login.configure(text="登录中…")
            self._set_status(design.AMBER, "登录中")
        elif kind == "live_login":
            self._set_status(design.AMBER, "登录直播")
        elif kind == "location":
            self.b_locate.configure(text="获取中…")
            self._location_hint.configure(text="正在获取 Windows 位置…", text_color=design.TXT3)
            self._set_status(design.AMBER, "正在定位")
        elif kind == "live_courses":
            self.b_course_refresh.configure(text="读取中…")
            self._set_status(design.AMBER, "读取直播课")
        else:
            self.b_test.configure(text="发送中…")
            self._set_status(design.AMBER, "测试通知")

        def run():
            from hdusign.live import LiveError
            failed = False
            result = None
            writer = QueueWriter(self.log_q)
            with redirect_stdout(writer), redirect_stderr(writer):
                try:
                    result = fn(*args)
                    failed = type(result) is int and result != 0
                except (LocationError, LiveError) as exc:
                    failed = True
                    result = exc
                    print(f"[!] {exc}")
                except SystemExit as exc:
                    failed = exc.code not in (None, 0)
                    if failed:
                        print(f"[错误] 任务退出：{exc.code}")
                except Exception as exc:  # noqa: BLE001
                    import traceback

                    failed = True
                    print(f"[错误] {exc}")
                    traceback.print_exc()
            self.task_q.put((kind, failed, result))

        self.worker = threading.Thread(target=run, daemon=True)
        self.worker.start()
        return True

    def _set_status(self, color, status):
        if self.__dict__.get("_status_signature") == (color, status):
            return
        self._status_signature = (color, status)
        # 属性值仅按内容撑开；监控成功、等待和异常各自使用轻量标签。
        self._status_key = "active" if color in (
            design.ACCENT, design.AMBER, design.RED) else "idle"
        background = design.INSET
        if status == "监控中":
            color, background = design.GREEN, design.GREEN_SOFT
        elif color == design.AMBER:
            background = design.AMBER_SOFT
        elif color == design.RED:
            background = design.RED_SOFT
        self._status_chip.configure(fg_color=background)
        self._dot.configure(text_color=color)
        self._status.configure(text=status, text_color=color)
        self._timer_label.configure(text="距离开课" if status == "已预约" else "已运行时长")

    def _pulse_dot(self):
        # 状态只在状态切换时重绘；同色呼吸不会提供反馈，反而增加 Tk 重绘。
        return

    def _finish_task(self, kind, failed, result=None):
        self._monitor_button_signature = None
        if self._closing:
            self._task_kind = None
            self.worker = None
            return
        if kind == "monitor":
            self._update_timer()
            if not self._has_transcript:
                self._transcript.configure(text=(
                    "监控已中断" if self._monitor_ready else "监控未能启动") if failed else "监控已结束")
            self._transcript_hint.configure(
                text="请查看下方活动记录，调整后重试。" if failed else "选择课程后点击「开始监控」开始下一次课堂。")
        elif kind == "location":
            if failed:
                message = str(result) if isinstance(result, LocationError) else "获取失败，请查看活动记录或手动填写。"
                self._location_hint.configure(text=message, text_color=design.AMBER)
            elif isinstance(result, Location):
                if (self.v_lat.get(), self.v_lng.get()) != self._location_request_values:
                    self._location_hint.configure(
                        text="坐标已手动修改，已保留当前填写。", text_color=design.TXT3)
                else:
                    self.v_lat.set(f"{result.lat:.6f}")
                    self.v_lng.set(f"{result.lng:.6f}")
                    for key in ("lat", "lng"):
                        self._entries[key].invalid = False
                        self._entries[key].configure(border_color=design.INPUT_BORDER)
                    accuracy = f"，误差约 {result.accuracy_m:.0f} 米" if result.accuracy_m is not None else ""
                    self._location_hint.configure(
                        text=f"已获取{accuracy}。请核对后保存。",
                        text_color=design.AMBER if result.accuracy_m is not None and result.accuracy_m > 1000 else design.TXT3)
                    self.logline(f"[OK] 已获取 Windows 当前位置{accuracy}，请核对后保存。")
        elif kind == "live_courses":
            if failed:
                from hdusign.live import LiveError
                message = str(result) if isinstance(result, LiveError) else "读取直播课程失败，请稍后重试。"
                self.v_live_course.set("未读取到课程，请登录或刷新")
                self._render_course_list()
                self._live_course_hint.configure(text=message, text_color=design.AMBER)
            else:
                self._set_live_course_choices(result)
        elif kind == "live_login":
            self._needs_live_login = failed or result != 0
            self._live_course_hint.configure(
                text="登录未完成，请核对账号密码后重试；需要验证时在浏览器完成。" if failed
                else "登录成功，正在读取课程…", text_color=design.AMBER if failed else design.ACCENT)
        self._task_kind = None
        self._t0 = None
        self.worker = None
        self._update_monitor_button()
        self.b_login.configure(state="normal", text="重新登录签到")
        for key in ("user", "pwd"):
            self._entries[key].configure(state="normal")
        self.b_test.configure(state="normal", text="测试推送")
        self.b_locate.configure(state="normal", text="获取当前位置")
        self.b_course_refresh.configure(state="normal", text="刷新")
        self._update_audio_mode()
        if failed:
            self._set_status(design.RED, "任务异常")
        elif kind == "monitor":
            self._set_status(design.TXT3, "已停止")
            self.logline("[i] 监控已停止。")
        else:
            self._set_status(design.TXT3, "就绪")
        if self._scheduled_course is not None:
            self._set_status(design.AMBER, "已预约")
        if kind == "live_login" and not failed and result == 0:
            self._set_account_form(False)
            self.refresh_live_courses()

    def toggle_monitor(self):
        if self._task_kind == "monitor":
            self.stop_monitor()
            return
        if self._busy():
            return
        if self._scheduled_course is not None:
            self._cancel_scheduled_course(notify=True)
            return
        selected = self._live_course_lookup.get(self.v_live_course.get())
        if selected is not None and selected.start > time.time():
            self._schedule_monitor(selected)
        else:
            self.start_monitor()

    def start_monitor(self):
        if self._busy():
            return False
        if self.v_live.get() and self._needs_live_login:
            self._select_tab("extras")
            self._feedback("账号信息已更改，请先登录并读取课程", error=True)
            return False
        if self.v_live.get() and not self._valid_url(self.v_url.get().strip()):
            self._select_tab("basic")
            self._feedback("请选择直播课程，再点击「开始监控」", error=True)
            return False
        if self.v_live.get():
            from hdusign.live import LiveError, parse_live_page
            try:
                parse_live_page(self.v_url.get().strip())
            except LiveError as exc:
                self._field_error("url", str(exc))
                return False
        if self._scheduled_course is not None:
            self._cancel_scheduled_course(notify=False)
        if not self.save_cfg():
            return False
        self.stop_event.clear()
        self._active_audio_source = "live" if self.v_live.get() else "system"

        def run(cfg):
            from hdusign import monitor

            if not self.stop_event.is_set():
                monitor.cmd_run(cfg, self.stop_event)

        return self._worker(run, copy.deepcopy(self.cfg), kind="monitor")

    def stop_monitor(self):
        if self._task_kind != "monitor" or self.stop_event.is_set():
            return
        self.stop_event.set()
        self.b_monitor.configure(state="disabled", text="正在停止…")
        self._set_status(design.AMBER, "正在停止")
        self.logline("[i] 正在停止监控…")

    def do_login(self):
        if self._busy() or not self.save_cfg():
            return

        def run():
            from hdusign import browser

            return browser.login()

        self._worker(run, kind="login")

    def do_live_login(self, switch_account=False):
        if self._busy():
            return
        if not switch_account:
            if not self.v_user.get().strip():
                self._field_error("user", "请输入学号 / 账号")
                return
            if not self.v_pwd.get():
                self._field_error("pwd", "请输入密码")
                return
        if self._scheduled_course is not None and not self._cancel_scheduled_course(notify=True):
            return
        if not self.save_cfg():
            return
        self.stop_event.clear()
        from hdusign.live import LiveError, parse_live_origin
        url = self.v_url.get().strip()
        try:
            parse_live_origin(url)
        except LiveError:
            url = "https://course.hdu.edu.cn/#/home"

        credentials = copy.deepcopy(self.secrets)
        self._needs_live_login = True
        # A new login must not leave a previous account's course selection active.
        self._live_courses = []
        self._live_course_lookup = {}
        self.v_url.set("")
        self.v_live_course.set("正在登录…")
        self._render_course_list()
        self._live_course_hint.configure(
            text="正在登录，需要验证码时请在浏览器中完成。", text_color=design.TXT3)

        def run(url):
            from hdusign.live import login_live

            if switch_account:
                return login_live(url, self.stop_event, switch_account=True)
            return login_live(url, self.stop_event, credentials=credentials)

        self._worker(run, url, kind="live_login")

    def test_webhook(self):
        if self._busy():
            return
        if not self._valid_url(self.v_hook.get().strip()):
            self._field_error("hook", "请填写有效的企业微信 Webhook 地址")
            return
        if not self.save_cfg():
            return

        def run(cfg):
            from hdusign import monitor

            monitor.cmd_webhook_test(cfg)

        self._worker(run, copy.deepcopy(self.cfg), kind="webhook")

    def locate(self):
        if self._busy():
            return
        self._location_request_values = (self.v_lat.get(), self.v_lng.get())
        self._worker(get_current_location, kind="location")

    @staticmethod
    def _valid_url(value):
        try:
            parsed = urlparse(value)
            return parsed.scheme in ("http", "https") and bool(parsed.hostname)
        except ValueError:
            return False

    def open_url(self):
        url = self.v_url.get().strip()
        if not self._valid_url(url):
            self._field_error("url", "请填写以 http:// 或 https:// 开头的直播网址")
            return
        webbrowser.open(url)

    def _update_timer(self):
        if self._t0 is not None:
            elapsed = int(time.monotonic() - self._t0)
            self._metrics["time"].configure(
                text=f"{elapsed // 3600:02d}:{elapsed // 60 % 60:02d}:{elapsed % 60:02d}")

    def _tick(self):
        self._update_timer()
        self._pulse_dot()
        self._check_scheduled_monitor()
        selected = self._live_course_lookup.get(self.v_live_course.get())
        if (self._task_kind is None and self._scheduled_course is None
                and selected is not None and selected.start <= time.time()
                and self.b_monitor.cget("text") == "预约直播"):
            self._update_monitor_button()
        self._tick_job = self.after(1000, self._tick)

    # ---------- 日志和实时转写 ----------
    def _set_code(self, code):
        if self._copy_job:
            self.after_cancel(self._copy_job)
            self._copy_job = None
        self._code = code
        active = bool(code)
        text = list(code) if code else ["—"] * 4
        for box, digit in zip(self._digit_boxes, text):
            desired = {"text": digit, "text_color": design.TXT if active else design.TXT3}
            changed = {key: value for key, value in desired.items() if box.cget(key) != value}
            if changed:
                box.configure(**changed)
        desired = dict(text="复制", state="normal" if code else "disabled",
                       text_color=design.TXT2,
                       image=self._icon("copy", 14, design.TXT2 if active else design.TXT3))
        changed = {key: value for key, value in desired.items() if self.b_copy.cget(key) != value}
        if changed:
            self.b_copy.configure(**changed)

    def copy_code(self):
        if not self._code:
            return False
        try:
            self.clipboard_clear()
            self.clipboard_append(self._code)
        except TclError:
            self._feedback("复制失败，请重试", error=True)
            return False
        if self._copy_job:
            self.after_cancel(self._copy_job)
        self.b_copy.configure(text="已复制", image=self._icon("check", 14))

        def reset_feedback():
            self._copy_job = None
            self._set_code(self._code)

        self._copy_job = self.after(1800, reset_feedback)
        return True

    def logline(self, s: str):
        self.log_q.put(s)

    def _append_log(self, line):
        self._append_logs((line,))

    def _show_log_transcript(self, text, color, hint):
        # 读取控件的当前值，启动、停止或重连后的状态也能正确覆盖。
        changes = {}
        if self._transcript.cget("text") != text:
            changes["text"] = text
        if self._transcript.cget("text_color") != color:
            changes["text_color"] = color
        if changes:
            self._transcript.configure(**changes)
        if self._transcript_hint.cget("text") != hint:
            self._transcript_hint.configure(text=hint)

    def _append_logs(self, lines):
        segments = []
        preview = None
        activity_changed = False
        for line in lines:
            line = line.strip()
            if not line:
                continue
            if line.startswith("…识别中:"):
                self._has_transcript = True
                preview = (line.partition(":")[2].strip(), design.TXT2, "")
                continue  # 同一批中间结果只显示最新内容，不写入最终日志。
            if line.startswith("[ASR "):
                self._has_transcript = True
                preview = (line.partition("]")[2].strip(), design.TXT, "")
            if (code := re.search(r"签到码[:：]\s*([0-9]{4})(?!\d)", line)):
                self._set_code(code.group(1))
            if not line.startswith("[ASR ") and (
                    code or "签到提醒" in line or "签到成功" in line or "签到失败" in line):
                self._record_activity(line)
                activity_changed = True
            if "ASR 就绪" in line and self._task_kind == "monitor" and not self.stop_event.is_set():
                self._monitor_ready = True
                self._set_status(design.ACCENT, "监控中")
                if not self._has_transcript:
                    preview = ("等待课堂声音…", design.TXT2,
                               "正在接收直播音频，无需网页播放。" if self._active_audio_source == "live"
                               else "正在接收电脑播放的声音。")
            if (line.startswith("[i] 直播状态:") and self._task_kind == "monitor"
                    and not self.stop_event.is_set()):
                reconnecting = "正在重连" in line
                self._set_status(design.AMBER, "正在重连" if reconnecting else "正在连接")
                self._has_transcript = False
                preview = ("直播暂时中断，正在重新连接…" if reconnecting else "正在连接直播…",
                           design.TXT2,
                           "恢复收到声音后会继续识别。" if reconnecting else "收到直播声音后开始识别。")
            if line.startswith("[错误]") or "Traceback" in line:
                tag = "error"
            elif line.startswith(("[!]", "[warn]")):
                tag = "warn"
            elif line.startswith("[OK]") or "签到提醒" in line:
                tag = "success"
            elif line.startswith("[ASR "):
                tag = "asr"
            else:
                tag = "info"
            display_line = re.sub(r"^\[(?:ASR [^\]]+|i|OK)\]\s*", "", line)
            segments.extend((time.strftime("%H:%M:%S") + "  ", ("time",), display_line + "\n", (tag,)))
        if segments:
            self._write_log_batch(segments)
        if preview is not None:
            self._show_log_transcript(*preview)
        if activity_changed:
            self._render_activity_records()

    def _write_log_batch(self, segments):
        self.log.configure(state="normal")
        try:
            # CTk 的包装方法只接收一段文本；Tk Text 支持一次插入多段及各自标签。
            self.log._textbox.insert("end", *segments)
            excess = int(self.log.index("end-1c").split(".")[0]) - 1 - MAX_LOG_LINES
            if excess > 0:
                self.log.delete("1.0", f"{excess + 1}.0")
            if self.v_follow.get():
                self.log.yview_moveto(1.0)
        finally:
            self.log.configure(state="disabled")
        self._empty_log.place_forget()

    def clear_log(self):
        # 清空日志视图，保留本次运行时间、签到码和磁盘记录。
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self._empty_log.place(relx=0.5, rely=0.5, anchor="center")
        self._activity_records.clear()
        self._render_activity_records()

    def _poll_log(self):
        # 留出约 2 ms 一次提交文本和预览，避免逐条 Tk 重绘占满事件循环。
        deadline = time.perf_counter() + 0.008

        def pending_lines():
            for count in range(160):
                if count and time.perf_counter() >= deadline:
                    break
                try:
                    yield self.log_q.get_nowait()
                except queue.Empty:
                    break

        self._append_logs(pending_lines())
        # 完成通知入队后再检查一次日志，覆盖工作线程恰好写入最后一条的竞态。
        if self.log_q.empty():
            completion = getattr(self, "_log_completion", None)
            if completion is None:
                try:
                    completion = self.task_q.get_nowait()
                except queue.Empty:
                    pass
            self._log_completion = completion
            if completion is not None and self.log_q.empty():
                self._log_completion = None
                self._finish_task(*completion)
        # 有积压时先让出事件循环，随后尽快继续；空闲时维持低频轮询。
        self._poll_job = self.after(10 if not self.log_q.empty() else 100, self._poll_log)

    def _on_close(self):
        if self._closing:
            return
        self._closing = True
        self.stop_event.set()
        self.b_monitor.configure(state="disabled", text="正在关闭…")
        self.b_login.configure(state="disabled")
        self.b_test.configure(state="disabled")
        self.b_locate.configure(state="disabled")
        self.b_url.configure(state="disabled")
        self.b_live_account.configure(state="disabled")
        self.b_live_login.configure(state="disabled")
        self.b_course_refresh.configure(state="disabled")
        self._set_status(design.AMBER, "正在关闭")
        self._wait_for_monitor_close()

    def _wait_for_monitor_close(self):
        # Wait for attendance cleanup or the bounded system-location helper.
        # Keep Tk responsive rather than abandoning a child process on exit.
        self._close_job = None
        if self._task_kind in ("monitor", "location", "live_login", "live_courses") and self.worker and self.worker.is_alive():
            self._close_job = self.after(100, self._wait_for_monitor_close)
        else:
            self.destroy()

    def destroy(self):
        self.stop_event.set()
        for job in (self._poll_job, self._tick_job, self._copy_job, self._close_job,
                    getattr(self, "_courses_job", None)):
            if job:
                self.after_cancel(job)
        super().destroy()


def main() -> int:
    App().mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
