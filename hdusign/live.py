"""Resolve one current HDU live stream and keep its login separate from sign-in."""
from __future__ import annotations

from dataclasses import dataclass, field
import datetime as dt
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import parse_qs, quote, unquote, urlsplit, urlunsplit

import requests
from playwright.sync_api import Error as PlaywrightError, sync_playwright

from hdusign.runtime import data_root as application_root, configure_browser_runtime


_SITE_HOSTS = frozenset(("course.hdu.edu.cn",
                        "https-course-hdu-edu-cn-443.webvpn.hdu.edu.cn"))
_SSO_HOSTS = frozenset(("sso.hdu.edu.cn", "cas.hdu.edu.cn",
                       "https-sso-hdu-edu-cn-443.webvpn.hdu.edu.cn",
                       "https-cas-hdu-edu-cn-443.webvpn.hdu.edu.cn"))
_API = "/jy-application-resourcemanage"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36")
_LOGIN_TIMEOUT_SECONDS = 180.0
_BEIJING = dt.timezone(dt.timedelta(hours=8))
_COOKIE_NAME = re.compile(r"[!#$%&'*+\-.^_\x60|~0-9A-Za-z]+\Z")
_UNSUPPORTED = "当前直播格式暂不支持，请使用电脑声音模式。"


class LiveError(RuntimeError):
    """A user-readable error that never contains a signed URL or credential."""

    retryable = False


class LiveTransientError(LiveError):
    """A temporary failure that may be retried for this same course only."""

    retryable = True


class LiveEnded(LiveError):
    """The requested live class has ended; do not retry or select another one."""


class LiveNotStarted(LiveError):
    """A scheduled stream may be retried within its bounded startup window."""


class LiveLoginRequired(LiveError):
    """The user needs to complete the separate live-platform login."""


@dataclass
class LiveStream:
    url: str = field(repr=False)
    headers: dict = field(repr=False)
    title: str = "直播音频"


@dataclass(frozen=True)
class LiveCourse:
    """One live lesson returned by the read-only course list endpoint."""

    course_id: str
    title: str
    start: float
    end: float
    teacher: str = ""
    classroom: str = ""
    section: str = ""
    tecl_id: str = ""
    origin: str = "https://course.hdu.edu.cn"
    subject_id: str = ""
    room_id: str = ""
    term_id: str = ""
    number: int = 0
    lesson_ids: tuple[str, ...] = ()
    lesson_starts: tuple[float, ...] = ()
    lesson_ends: tuple[float, ...] = ()

    def __post_init__(self):
        # YAML/JSON restore arrays as lists. Keep scheduled and fresh courses
        # identical and immutable when passed between GUI and worker threads.
        for name in ("lesson_ids", "lesson_starts", "lesson_ends"):
            value = getattr(self, name)
            if not isinstance(value, (list, tuple)):
                raise ValueError("连堂课资料无效，请重新刷新课程。")
            object.__setattr__(self, name, tuple(value))


def _plain(value: object) -> bool:
    return (isinstance(value, str)
            and not any(ord(char) < 32 or ord(char) == 127 for char in value))


def _url(value: object):
    if not _plain(value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.username is not None or parsed.password is not None:
            return None
        parsed.port
        return parsed
    except ValueError:
        return None


def parse_live_page(page_url: str) -> tuple[str, str]:
    """Return the allowed origin and one course ID; reject replay/share routes."""
    page = _url(page_url)
    if (page is None or page.scheme != "https" or page.hostname not in _SITE_HOSTS
            or page.port not in (None, 443)):
        raise LiveError("请填写杭电课堂直播页面的网址。")
    route = _url(page.fragment) if page.fragment else page
    if route is None:
        raise LiveError("请使用正在上课的直播链接；该链接不是有效的单节直播页面。")
    query = parse_qs(route.query, keep_blank_values=True)
    ids = query.get("courseId", [])
    if (route.path not in ("/play-center", "/play-live")
            or query.get("target") != ["live"] or len(ids) != 1
            or re.fullmatch(r"[0-9]{1,20}", ids[0]) is None or int(ids[0]) <= 0):
        raise LiveError("请使用正在上课的直播链接；该链接不是有效的单节直播页面。")
    return "https://" + page.hostname, str(int(ids[0]))


def parse_live_origin(page_url: str) -> str:
    """Return a trusted course-site origin from a page or homepage URL."""
    page = _url(page_url)
    if (page is None or page.scheme != "https" or page.hostname not in _SITE_HOSTS
            or page.port not in (None, 443)):
        raise LiveError("请填写杭电课堂网址。")
    if page.path not in ("", "/") or page.query:
        try:
            return parse_live_page(page_url)[0]
        except LiveError:
            # A hash route such as /#/home is still a valid login entry page.
            if page.fragment and page.path in ("", "/"):
                return "https://" + page.hostname
            raise
    return "https://" + page.hostname


def live_course_url(course: LiveCourse) -> str:
    """Build the canonical page URL consumed by the existing live client."""
    origin = _url(course.origin)
    course_id = str(getattr(course, "course_id", ""))
    tecl_id = str(getattr(course, "tecl_id", "") or "")
    if (origin is None or origin.scheme != "https" or origin.hostname not in _SITE_HOSTS
            or origin.port not in (None, 443) or origin.path not in ("", "/")
            or origin.query or origin.fragment
            or re.fullmatch(r"[0-9]{1,20}", course_id) is None
            or int(course_id) <= 0):
        raise LiveError("直播课程资料无效，请重新刷新课程列表。")
    query = ["courseId=" + quote(str(int(course_id)), safe=""),
             "liveId=" + quote(str(int(course_id)), safe="")]
    if tecl_id:
        if re.fullmatch(r"[0-9]{1,20}", tecl_id) is None or int(tecl_id) <= 0:
            raise LiveError("直播课程资料无效，请重新刷新课程列表。")
        query.append("teclId=" + quote(str(int(tecl_id)), safe=""))
    query.append("target=live")
    return "https://" + origin.hostname + "/#/play-center?" + "&".join(query)


def _domain_matches(domain: str, host: str) -> bool:
    base = domain.lstrip(".").lower()
    return host == base or (domain.startswith(".") and host.endswith("." + base))


def _cookies_for_origin(raw: object, origin: str) -> list[dict]:
    host = urlsplit(origin).hostname
    result = []
    for cookie in raw if isinstance(raw, list) else []:
        if not isinstance(cookie, dict):
            continue
        name, value = cookie.get("name"), cookie.get("value")
        domain, path = cookie.get("domain"), cookie.get("path", "/")
        if (not isinstance(name, str) or not _COOKIE_NAME.fullmatch(name)
                or not _plain(value) or ";" in value or not _plain(domain)
                or not (domain.lstrip(".") == "hdu.edu.cn"
                        or domain.lstrip(".").endswith(".hdu.edu.cn"))
                or not _domain_matches(domain, host)
                or not _plain(path) or not path.startswith("/")):
            continue
        expiry = cookie.get("expires")
        if expiry not in (None, -1):
            if (isinstance(expiry, bool) or not isinstance(expiry, (int, float))
                    or not math.isfinite(expiry) or expiry <= time.time()):
                continue
        result.append({"name": name, "value": value, "domain": domain,
                       "path": path, "secure": bool(cookie.get("secure", False)),
                       "expires": expiry})
    return result


def _timestamp(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        if isinstance(value, str) and not re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", value):
            when = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            result = when.replace(tzinfo=_BEIJING).timestamp() if when.tzinfo is None else when.timestamp()
        else:
            result = float(value)
            if abs(result) >= 100_000_000_000:
                result /= 1000
        return result if math.isfinite(result) and 0 <= result < 32_503_680_000 else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _status(value: object, expected: int) -> bool:
    return not isinstance(value, bool) and value in (expected, str(expected))


def _transient_status(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, str) and re.fullmatch(r"[0-9]{3}", value):
        value = int(value)
    return isinstance(value, int) and (value in (408, 429) or 500 <= value <= 599)


def _saved_auth(origin: str | None = None) -> tuple[str, list[dict], str]:
    auth = None
    try:
        auth = json.loads((application_root() / "live_session.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        pass
    saved_origin = auth.get("origin") if isinstance(auth, dict) else None
    parsed = _url(saved_origin)
    if (parsed is None or parsed.scheme != "https" or parsed.hostname not in _SITE_HOSTS
            or parsed.port not in (None, 443) or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment):
        raise LiveLoginRequired("请先点击“登录并读取课程”，完成本站登录。")
    saved_origin = "https://" + parsed.hostname
    if origin is not None and origin != saved_origin:
        raise LiveLoginRequired("请先点击“登录并读取课程”，完成本站登录。")
    cookies = _cookies_for_origin(auth.get("cookies"), saved_origin)
    jwt = auth.get("jwt_token", "")
    if not _plain(jwt) or not jwt.isascii():
        raise LiveLoginRequired("直播登录资料无效，请重新点击“登录并读取课程”。")
    if not cookies and not jwt:
        raise LiveLoginRequired("直播登录已失效，请重新点击“登录并读取课程”。")
    return saved_origin, cookies, jwt


def _authenticated_session(origin: str, config: dict | None = None) -> tuple[requests.Session, list[dict]]:
    _, cookies, jwt = _saved_auth(origin)
    session = requests.Session()
    # Do not implicitly load unrelated .netrc credentials. Retain the user's
    # proxy preference explicitly, without changing global network settings.
    session.trust_env = False
    options = (config or {}).get("browser") or {}
    if not options.get("bypass_proxy", False):
        session.proxies.update(requests.utils.get_environ_proxies(origin))
    session.headers.update({"User-Agent": _UA,
                            "Accept": "application/json, text/plain, */*",
                            "Referer": origin + "/"})
    if jwt:
        session.headers["jwt-token"] = jwt
    for cookie in cookies:
        session.cookies.set(
            cookie["name"], cookie["value"], domain=cookie["domain"], path=cookie["path"],
            secure=cookie["secure"],
            expires=int(cookie["expires"]) if cookie["expires"] not in (None, -1) else None)
    return session, cookies


def _checked_payload(response, *, action: str) -> dict:
    try:
        if response.status_code in (301, 302, 303, 307, 308, 401):
            raise LiveLoginRequired("请先点击“登录并读取课程”，完成本站登录。")
        if response.status_code == 403:
            raise LiveError("当前账号无权读取直播课程。" if action == "list" else "当前账号无权观看这节直播。")
        if _transient_status(response.status_code):
            raise LiveTransientError("课堂直播平台暂时无法读取课程，请稍后重试。" if action == "list"
                                     else "课堂直播平台暂时无法提供音频，请稍后重试。")
        if response.status_code != 200:
            raise LiveError("课堂直播平台暂时无法读取课程，请稍后重试。" if action == "list"
                            else "课堂直播平台暂时无法提供音频，请稍后重试。")
        payload = None
        try:
            payload = response.json()
        except (ValueError, TypeError):
            pass
        if not isinstance(payload, dict):
            raise LiveError("无法解析直播课程，请重新登录直播后重试。" if action == "list"
                            else "无法解析直播状态，请重新登录直播后重试。")
        return payload
    finally:
        response.close()


def _list_origin(config: dict | None) -> str:
    value = str((config or {}).get("live_url") or "").strip()
    if value:
        try:
            return parse_live_origin(value)
        except LiveError:
            pass
    return _saved_auth()[0]


def _text(value: object) -> str:
    return value.strip() if _plain(value) else ""


def _teacher(record: dict) -> str:
    names = record.get("teacNames")
    if isinstance(names, list):
        clean = [_text(name) for name in names]
        return "、".join(name for name in clean if name)
    return _text(record.get("tecName"))


def _normalise_course(record: object, origin: str) -> LiveCourse | None:
    if not isinstance(record, dict):
        return None
    course_id = record.get("id", record.get("courseId"))
    if (isinstance(course_id, bool) or re.fullmatch(r"[0-9]{1,20}", str(course_id or "")) is None
            or int(course_id) <= 0):
        return None
    start, end = _timestamp(record.get("courBeginTime")), _timestamp(record.get("courEndTime"))
    if start is None or end is None or end <= start:
        return None
    title = _text(record.get("courName")) or _text(record.get("subjName"))
    if not title:
        return None
    tecl_id = record.get("teclId")
    if (isinstance(tecl_id, bool) or tecl_id in (None, "")
            or re.fullmatch(r"[0-9]{1,20}", str(tecl_id)) is None or int(tecl_id) <= 0):
        tecl_id = ""
    else:
        tecl_id = str(int(tecl_id))
    section = ""
    number = record.get("letiNumber")
    if not isinstance(number, bool) and isinstance(number, (int, float, str)):
        raw = str(number).strip()
        if re.fullmatch(r"[0-9]{1,3}(?:\.0+)?", raw) and int(float(raw)) > 0:
            section = f"第{int(float(raw))}节"
    def identifier(key):
        value = record.get(key)
        return str(int(value)) if (not isinstance(value, bool)
                                   and re.fullmatch(r"[0-9]{1,20}", str(value or ""))
                                   and int(value) > 0) else ""
    return LiveCourse(str(int(course_id)), title, start, end, _teacher(record),
                      _text(record.get("clroName")), section, tecl_id, origin,
                      subject_id=identifier("subjId"), room_id=identifier("clroId"),
                      term_id=identifier("acteId"),
                      number=int(float(raw)) if section else 0)


def _course_list_page(session, origin: str, page: int, live_day: int = 0,
                      *, timetable=None) -> dict:
    params = {"page.pageIndex": page, "page.pageSize": 1000,
              "page.orders[0].asc": "true", "page.orders[0].field": "courBeginTime"}
    endpoint = "/v1/vod_live/t-1"
    if timetable is None:
        params["liveDay"] = live_day
    else:
        term, first, last = timetable
        endpoint = "/v1/myself/curriculum"
        params.update(acteId=term, courBeginTime=first, courEndTime=last)
    try:
        response = session.get(
            origin + _API + endpoint, params=params,
            timeout=(10, 15), allow_redirects=False)
    except requests.exceptions.SSLError:
        raise LiveError("课堂直播平台的安全连接验证失败，请检查系统时间或联系平台。") from None
    except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError):
        raise LiveTransientError("无法连接课堂直播平台，请检查网络后重试。") from None
    except (requests.RequestException, OSError, ValueError):
        raise LiveError("无法读取直播课程，请检查网络和直播登录状态。") from None
    payload = _checked_payload(response, action="list")
    if _status(payload.get("status"), 401) or _status(payload.get("code"), 401):
        raise LiveLoginRequired("直播登录已失效，请重新点击“登录并读取课程”。")
    if _status(payload.get("status"), 403) or _status(payload.get("code"), 403):
        raise LiveError("当前账号无权读取直播课程。")
    if _transient_status(payload.get("status")) or _transient_status(payload.get("code")):
        raise LiveTransientError("课堂直播平台暂时无法读取课程，请稍后重试。")
    data = payload.get("data")
    if (not _status(payload.get("status"), 200) or payload.get("ok") is False
            or not isinstance(data, dict) or not isinstance(data.get("records"), list)):
        raise LiveError("课堂直播平台未返回有效课程列表，请重新登录直播后重试。")
    return data


def _now() -> float:
    return time.time()


def _read_course_pages(session, origin, stop, timetable=None):
    unique = {}
    received = 0
    previous_ids = None
    for page in range(1, 21):
        if stop is not None and stop.is_set():
            raise LiveError("读取直播课程已取消。")
        data = (_course_list_page(session, origin, page) if timetable is None else
                _course_list_page(session, origin, page, timetable=timetable))
        if stop is not None and stop.is_set():
            raise LiveError("读取直播课程已取消。")
        records = data["records"]
        received += len(records)
        courses = [course for course in (_normalise_course(record, origin) for record in records)
                   if course is not None]
        if timetable is not None and len(courses) != len(records):
            raise LiveError("完整课表含无效节次，暂不能识别连堂课。")
        ids = tuple(course.course_id for course in courses)
        if records and ids == previous_ids:
            raise LiveError("直播课程分页未更新，请稍后刷新。")
        previous_ids = ids
        unique.update((course.course_id, course) for course in courses)
        # HDU's real pagination uses rowCount; retain total for older responses.
        total = data.get("total", data.get("rowCount"))
        if (not records or (type(total) is int and 0 <= total <= received)
                or (type(total) is not int and len(records) < 1000)):
            break
    else:
        raise LiveError("直播课程过多，暂时无法完整读取，请稍后刷新或手动填写网址。")
    return unique


def list_live_courses(config: dict | None = None, *, stop=None) -> list[LiveCourse]:
    """Read live entries and optionally recover all cells from the own timetable."""
    origin = _list_origin(config)
    session, cookies = _authenticated_session(origin, config)
    requested_at = _now()
    try:
        unique = _read_course_pages(session, origin, stop)

        current = dt.datetime.fromtimestamp(requested_at, _BEIJING).date()
        visible_dates = {current, current + dt.timedelta(days=1)}
        continuous = bool(((config or {}).get("live_audio") or {}).get("continuous", False))
        grouping_available = not continuous
        if continuous:
            terms = sorted({c.term_id for c in unique.values() if c.term_id})
            try:
                if not terms or len(terms) > 3:
                    raise LiveError("直播列表缺少完整学期信息。")
                timetable_courses = {}
                first, last = f"{current} 00:00:00", f"{current + dt.timedelta(days=1)} 23:59:59"
                for term in terms:
                    timetable_courses.update(_read_course_pages(session, origin, stop, (term, first, last)))
                # Restrict to teaching classes already visible to this account
                # in the live list, including their earlier timetable cells.
                keys = {(c.tecl_id, c.subject_id, c.term_id) for c in unique.values()}
                timetable_courses = {cid: c for cid, c in timetable_courses.items()
                                     if (c.tecl_id, c.subject_id, c.term_id) in keys}
                for cid, course in timetable_courses.items():
                    previous = unique.get(cid)
                    if previous is not None and (previous.tecl_id, previous.start, previous.end,
                                                 previous.subject_id, previous.room_id, previous.term_id) != (
                            course.tecl_id, course.start, course.end,
                            course.subject_id, course.room_id, course.term_id):
                        raise LiveError("直播列表与课表资料不一致。")
                # A live entry absent from the complete timetable cannot be
                # assumed to be the first cell of a complete class.
                verified = {cid: c for cid, c in timetable_courses.items()}
                missing = [c for cid, c in unique.items() if cid not in verified]
                unique = verified
                grouping_available = True
            except LiveLoginRequired:
                raise
            except LiveError as exc:
                if stop is not None and stop.is_set():
                    raise
                print(f"[!] 完整课表读取失败，当前只提供单节监控：{exc}")
                missing = []
        else:
            missing = []
        courses = [course for course in unique.values()
                   if dt.datetime.fromtimestamp(course.start, _BEIJING).date() in visible_dates]
        from hdusign.lessons import group_lessons
        result = group_lessons(courses) if grouping_available else courses
        result.extend(c for c in missing
                      if dt.datetime.fromtimestamp(c.start, _BEIJING).date() in visible_dates)
        return sorted(result, key=lambda c: (c.start, c.end, c.course_id))
    finally:
        session.headers.pop("jwt-token", None)
        session.cookies.clear()
        session.close()
        cookies.clear()


class LiveClient:
    """Only GET the live-info endpoint; never send viewing records or heartbeats."""

    parse_live_page = staticmethod(parse_live_page)

    def __init__(self, page_url: str, config: dict | None = None):
        self.origin, self.course_id = parse_live_page(page_url)
        self._session, self._cookies = _authenticated_session(self.origin, config)
        self._closed = False
        self.lesson_end = None

    def select_lesson(self, course_id: str, end: float) -> None:
        """Set a verified timetable block; the first stream covers two lessons."""
        if not re.fullmatch(r"[1-9][0-9]{0,19}", course_id) or not math.isfinite(end):
            raise LiveError("连堂课资料无效，请重新选择课程。")
        self.course_id = course_id
        self.lesson_end = end

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._session.close()
            self._session.headers.pop("jwt-token", None)
            self._session.cookies.clear()
            self._cookies.clear()

    def _get_info(self) -> dict:
        response = None
        failure_type = LiveError
        failure_message = "无法连接课堂直播平台，请检查网络后重试。"
        try:
            response = self._session.get(
                self.origin + _API + "/v1/course_vod_videoinfos",
                params={"courseId": self.course_id, "playType": 2},
                timeout=(10, 15), allow_redirects=False)
        except requests.exceptions.SSLError:
            failure_message = "课堂直播平台的安全连接验证失败，请检查系统时间或联系平台。"
        except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError):
            failure_type = LiveTransientError
        except (requests.RequestException, OSError, ValueError):
            pass
        if response is None:
            raise failure_type(failure_message)
        return _checked_payload(response, action="resolve")

    def resolve(self) -> LiveStream:
        if self._closed:
            raise LiveError("直播连接已关闭，请重新开始监控。")
        payload = self._get_info()
        if _status(payload.get("status"), 401) or _status(payload.get("code"), 401):
            raise LiveLoginRequired("直播登录已失效，请重新点击“登录并读取课程”。")
        if _status(payload.get("status"), 403) or _status(payload.get("code"), 403):
            raise LiveError("当前账号无权观看这节直播。")
        info = payload.get("data")
        if isinstance(info, dict) and _status(info.get("lvcrLiveStatus"), 0):
            raise LiveError("当前账号无权观看这节直播。")
        # The platform uses business status 501 for "live has not started";
        # it is not the same condition as an HTTP 501 server failure.
        if _status(payload.get("status"), 501):
            raise LiveNotStarted("这节直播尚未开始，请开播后再试。")
        if _transient_status(payload.get("status")) or _transient_status(payload.get("code")):
            raise LiveTransientError("课堂直播平台暂时无法提供音频，请稍后重试。")
        if (not _status(payload.get("status"), 200) or payload.get("ok") is False
                or not isinstance(info, dict)):
            raise LiveError("课堂直播平台未提供有效直播状态，请在网页确认这节课已开播。")
        if info.get("errorMsg"):
            raise LiveError("课堂直播平台未提供可播放的音频，请在网页确认直播状态。")
        if info.get("id") is not None and str(info["id"]) != self.course_id:
            raise LiveError("直播课程与链接不一致，请重新复制当前直播链接。")
        start, end = _timestamp(info.get("liveStartTime")), _timestamp(info.get("liveEndTime"))
        now = _timestamp(payload.get("timestamp"))
        now = time.time() if now is None else now
        if ((info.get("liveStartTime") not in (None, "") and start is None)
                or (info.get("liveEndTime") not in (None, "") and end is None)
                or (start is not None and end is not None and end <= start)):
            raise LiveError("无法确认当前直播时段，请在网页确认这节课正在直播。")
        if start is not None and now < start:
            raise LiveNotStarted("这节直播尚未开始，请开播后再试。")
        # Only an explicitly verified two/three-lesson plan can extend the
        # first cell's window. Authentication/permission/format checks remain.
        effective_end = self.lesson_end if self.lesson_end is not None else end
        if effective_end is not None and now >= effective_end:
            raise LiveEnded("这节直播已经结束，请使用当前正在上课的直播链接。")
        views = info.get("courseDeviceViewDtoList")
        if not isinstance(views, list):
            raise LiveError("直播平台未返回设备信息，请在网页确认直播状态。")
        if not views:
            raise LiveTransientError("这节直播暂时没有音频，请稍后重试。")
        for view in views:
            if not isinstance(view, dict):
                continue
            media = _url(view.get("chanNameMainPlayUrl"))
            if (media is None or media.scheme not in ("http", "https") or not media.hostname
                    or media.fragment or Path(media.path).suffix.lower() not in ("", ".flv")):
                continue
            token = view.get("mainTokenStr", "")
            if token is None:
                token = ""
            if not _plain(token):
                continue
            query = media.query
            if token:
                parts = [part for part in query.split("&") if part
                         and unquote(part.split("=", 1)[0]) != "account_token"]
                parts.append("account_token=" + quote(token, safe=""))
                query = "&".join(parts)
            url = urlunsplit((media.scheme, media.netloc, media.path, query, ""))
            # The official FLV client authenticates with account_token. Passing
            # Cookie/JWT as raw FFmpeg headers would also forward them on redirects.
            headers = {"User-Agent": _UA, "Referer": self.origin + "/"}
            return LiveStream(url, headers)
        raise LiveError(_UNSUPPORTED)


_PROBE_LOGIN_JS = """
({origin}) => {
    if (location.origin !== origin) return {ok: false};
    if (/^#\\/?login(?:[/?]|$)/i.test(location.hash)) return {ok: false};
    try {
        const keys = Object.keys(sessionStorage).filter(k => k.endsWith("_STORAGE_KEY_JWT_TOKEN"));
        const jwt = keys.length === 1 ? sessionStorage.getItem(keys[0]) || "" : "";
        return {ok: !!jwt, jwt_token: jwt};
    } catch (_) {
        return {ok: false};
    }
}
"""


def _login_candidate(page, origin):
    """Read browser storage only; never run authenticated fetch in a page."""
    current = _url(page.url)
    if (current is None or current.scheme != "https" or current.port not in (None, 443)
            or "https://" + current.hostname != origin
            or re.match(r"^/?login(?:[/?]|$)", current.fragment, re.I)):
        return ""
    result = page.evaluate(_PROBE_LOGIN_JS, {"origin": origin})
    jwt = result.get("jwt_token", "") if isinstance(result, dict) and result.get("ok") is True else ""
    return jwt if _plain(jwt) and jwt.isascii() else ""


def _verify_browser_login(context, origin, jwt):
    """APIRequestContext handles a 401 without Chromium's Basic Auth dialog."""
    response = None
    try:
        response = context.request.get(
            origin + _API + "/v1/currentuser", headers={"Accept": "application/json", "jwt-token": jwt},
            timeout=4000, max_redirects=0, fail_on_status_code=False)
        if response.status in (401, 403):
            return {"ok": False, "rejected": True}
        if response.status != 200:
            return {"ok": False}
        body = response.json()
        if not isinstance(body, dict):
            return {"ok": False}
        if _status(body.get("status"), 401) or _status(body.get("code"), 401):
            return {"ok": False, "rejected": True}
        data = body.get("data")
        if (body.get("ok") is not True or not _status(body.get("status"), 200)
                or not isinstance(data, dict) or data.get("id") is None or not str(data["id"]).strip()):
            return {"ok": False}
        return {"ok": True,
                "display_name": data["userName"].strip() if isinstance(data.get("userName"), str) else "",
                "user_code": str(data.get("userCode") or "").strip()}
    except (PlaywrightError, ValueError):
        return {"ok": False}
    finally:
        if response is not None:
            try:
                response.dispose()
            except PlaywrightError:
                pass


def _block_viewing_writes(route) -> None:
    """Login may visit a list page, but must not generate playback telemetry."""
    request = route.request
    parsed = _url(request.url)
    path = parsed.path if parsed is not None else ""
    blocked = (parsed is not None and parsed.hostname in _SITE_HOSTS
               and request.method.upper() not in ("GET", "HEAD", "OPTIONS")
               and (path.startswith(_API + "/v1/live/")
                    or path.startswith(_API + "/v1/vod/")
                    or path in (_API + "/v1/data/reporting",
                                _API + "/v1/statistics/visitRecord/create")))
    route.abort() if blocked else route.continue_()


def _saved_manual_login(origin: str) -> bool:
    try:
        saved = json.loads((application_root() / "live_session.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return False
    return (isinstance(saved, dict) and saved.get("origin") == origin
            and saved.get("login_mode") == "manual")


def _save_session(origin: str, cookies: list[dict], jwt: str,
                  *, login_mode: str | None = None, username: str | None = None,
                  display_name: str = "", user_code: str = "") -> None:
    root = application_root()
    temporary = None
    failed = False
    try:
        descriptor, name = tempfile.mkstemp(prefix=".live-session-", suffix=".json", dir=root)
        temporary = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            saved = {"version": 1, "origin": origin, "captured_at": time.time(),
                     "cookies": cookies, "jwt_token": jwt}
            if login_mode == "manual":
                saved["login_mode"] = "manual"
            if username is not None:
                saved["username"] = username
            if isinstance(display_name, str) and display_name.strip():
                saved["display_name"] = display_name.strip()[:100]
            if isinstance(user_code, str) and user_code.strip():
                saved["user_code"] = user_code.strip()[:100]
            json.dump(saved, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, root / "live_session.json")
    except (OSError, ValueError):
        failed = True
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                failed = True
    if failed:
        raise LiveError("无法保存直播登录资料，请检查程序目录的写入权限。")


def notification_name(config: dict) -> str:
    """Read the authenticated account's official name, never infer from a title."""
    context = config.get("notification_context") or {}
    if not isinstance(context, dict):
        return ""
    expected = str(context.get("username") or "").strip()
    try:
        origin = parse_live_origin(str(config.get("live_url") or "https://course.hdu.edu.cn/"))
        saved = json.loads((application_root() / "live_session.json").read_text("utf-8-sig"))
        if not isinstance(saved, dict) or saved.get("origin") != origin:
            return ""
        if expected and saved.get("username", saved.get("user_code")) != expected:
            return ""
        if (isinstance(saved.get("display_name"), str) and saved.get("user_code")
                and (not expected or saved["user_code"] == expected)):
            return saved["display_name"].strip()[:100]
        # Older sessions have no profile name. Resolve it once using the same
        # authenticated read-only endpoint, without making the user log in again.
        session, _ = _authenticated_session(origin, config)
        try:
            response = session.get(origin + _API + "/v1/currentuser",
                                   timeout=(3, 4), allow_redirects=False)
            if response.status_code != 200:
                return ""
            body = response.json()
            if not isinstance(body, dict) or body.get("ok") is not True or not _status(body.get("status"), 200):
                return ""
            data = body.get("data")
            if not isinstance(data, dict) or data.get("id") in (None, ""):
                return ""
            code = str(data.get("userCode") or "").strip()
            name = data.get("userName")
            if not isinstance(name, str) or not name.strip() or (expected and code != expected):
                return ""
            return name.strip()[:100]
        finally:
            session.cookies.clear()
            session.headers.pop("jwt-token", None)
            session.close()
    except (OSError, ValueError, LiveError, requests.RequestException):
        return ""


def login_live(page_url: str, stop=None, *, switch_account: bool = False,
               credentials: dict | None = None) -> int:
    """Explicit credentials use a fresh context; manual logins remain available."""
    origin = parse_live_origin(page_url)
    if stop is not None and stop.is_set():
        print("直播登录已取消。")
        return 1
    manual_login = switch_account or (credentials is None and _saved_manual_login(origin))
    from hdusign import browser

    configure_browser_runtime()
    context = None
    live_browser = None
    try:
        with sync_playwright() as playwright:
            try:
                if manual_login or credentials is not None:
                    live_browser = playwright.chromium.launch(
                        headless=False, args=browser._browser_args(), timeout=30000)
                    context = live_browser.new_context()
                    print("请在新窗口手动登录要使用的直播账号。" if manual_login
                          else "正在使用填写的账号登录，需要验证码时请在窗口中完成。")
                else:
                    context = playwright.chromium.launch_persistent_context(
                        str(application_root() / "live_profile"), headless=False,
                        args=browser._browser_args(), timeout=30000)
                context.route("**/*", _block_viewing_writes)
                page = context.pages[0] if context.pages else context.new_page()
                try:
                    page.goto(origin + "/#/list-live", wait_until="domcontentloaded", timeout=10000)
                except PlaywrightError:
                    pass
                deadline = time.monotonic() + _LOGIN_TIMEOUT_SECONDS
                attempted = False
                fill_attempts = 0
                probe_token, probe_attempts, next_probe_at = "", 0, 0.0
                rejected_tokens = set()
                while time.monotonic() < deadline:
                    if stop is not None and stop.is_set():
                        print("直播登录已取消。")
                        return 1
                    if page.is_closed():
                        print("直播登录窗口已关闭，尚未保存登录。")
                        return 1
                    current = _url(page.url)
                    if (current is not None and current.scheme == "https"
                            and current.port in (None, 443) and current.hostname in _SSO_HOSTS):
                        if not manual_login and not attempted and fill_attempts < 2:
                            try:
                                page.wait_for_selector("input[type=password]", timeout=1000)
                                secrets = credentials if credentials is not None else browser.load_secrets()
                                ready = _url(page.url)
                                if (ready is not None and ready.scheme == "https"
                                        and ready.port in (None, 443)
                                        and ready.hostname in _SSO_HOSTS
                                        and (stop is None or not stop.is_set())):
                                    fill_attempts += 1
                                    attempted = browser.try_sso_login(page, secrets)
                            except PlaywrightError:
                                pass
                            except (Exception, SystemExit):
                                attempted = True
                                print("请在直播登录窗口中完成登录和验证码。")
                    elif (current is not None and current.scheme == "https"
                          and current.port in (None, 443)
                          and current.hostname in _SITE_HOSTS
                          and "https://" + current.hostname == origin):
                        try:
                            jwt = _login_candidate(page, origin)
                            if stop is not None and stop.is_set():
                                print("直播登录已取消。")
                                return 1
                            if jwt and jwt != probe_token:
                                probe_token, probe_attempts, next_probe_at = jwt, 0, 0.0
                            result = {"ok": False}
                            if (jwt and jwt not in rejected_tokens
                                    and time.monotonic() >= next_probe_at):
                                probe_attempts += 1
                                next_probe_at = time.monotonic() + min(2 ** min(probe_attempts, 4), 15)
                                result = _verify_browser_login(context, origin, jwt)
                                if result.get("rejected"):
                                    rejected_tokens.add(jwt)
                                    print("[!] 当前登录凭据已失效，请在窗口中重新登录；取得新凭据后会继续验证。")
                            if stop is not None and stop.is_set():
                                print("直播登录已取消。")
                                return 1
                            if result.get("ok") is True and _login_candidate(page, origin) == jwt:
                                cookies = _cookies_for_origin(context.cookies(
                                    [origin + "/", origin + _API + "/v1/currentuser"]), origin)
                                if stop is not None and stop.is_set():
                                    print("直播登录已取消。")
                                    return 1
                                if _login_candidate(page, origin) != jwt:
                                    page.wait_for_timeout(250)
                                    continue
                                if _plain(jwt) and jwt.isascii() and (jwt or cookies):
                                    identity = ({"username": str(credentials.get("skl_username", "")).strip()}
                                                if credentials is not None and not manual_login else {})
                                    _save_session(origin, cookies, jwt,
                                                  login_mode="manual" if manual_login else None,
                                                  display_name=result.get("display_name", ""),
                                                  user_code=result.get("user_code", ""), **identity)
                                    print("直播登录已保存，可启动后台音频监控。")
                                    return 0
                        except PlaywrightError:
                            pass
                    page.wait_for_timeout(250)
                print("直播登录超时，请重新点击“登录并读取课程”并完成验证。")
                return 1
            finally:
                try:
                    if context is not None:
                        try:
                            context.close()
                        except PlaywrightError:
                            pass
                finally:
                    if live_browser is not None:
                        try:
                            live_browser.close()
                        except PlaywrightError:
                            pass
    except PlaywrightError:
        pass
    if stop is not None and stop.is_set():
        print("直播登录已取消。")
        return 1
    raise LiveError("无法完成直播登录，请关闭之前的直播登录窗口后重试。")
