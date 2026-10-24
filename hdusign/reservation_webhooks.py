"""Per-course notification endpoints and fail-closed worker route files."""
from __future__ import annotations

import hashlib
import math
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from hdusign.accounts import protect, read_json
from hdusign.lessons import EARLY_START, LATE_END


def listening_window(course):
    """Half-open interval for a whole lesson block, including pre/post roll.

    Missing or invalid timetable data cannot establish that two tasks are safe
    to share a robot. Callers treat an unknown interval as a conflict.
    """
    if course is None:
        return None
    start, end = course.start, course.end
    if (any(isinstance(v, bool) or not isinstance(v, (int, float))
            or not math.isfinite(v) for v in (start, end)) or end <= start):
        return None
    return start - EARLY_START, end + LATE_END


def windows_overlap(first, second):
    return (first is None or second is None
            or (first[0] < second[1] and second[0] < first[1]))


def webhook_key(url):
    """Compare actual endpoints, including aliases with reordered query args."""
    if not url:
        return ""
    parts = urlsplit(url.strip())
    query = parse_qsl(parts.query, keep_blank_values=True)
    keys = [value for key, value in query if key == "key"]
    if parts.hostname == "qyapi.weixin.qq.com" and parts.path.rstrip("/") == "/cgi-bin/webhook/send" and len(keys) == 1:
        normalized = "wecom:" + keys[0]
    else:
        normalized = urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path,
                                urlencode(sorted(query)), ""))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def binding(name="", url="", *, pending=False, reason=""):
    return {"mode": "pending" if pending else "bound" if url else "local",
            "key": webhook_key(url) if not pending else "",
            "name": "待选择机器人" if pending else name if url else "仅本地提醒",
            "encrypted_url": protect(url) if url and not pending else "", "reason": reason}


def binding_url(value):
    if not isinstance(value, dict) or value.get("mode") != "bound":
        return ""
    try:
        url = protect(value["encrypted_url"], True)
        return url if webhook_key(url) == value.get("key") else ""
    except (OSError, ValueError, KeyError):
        return ""


def route_resolver(root, specification):
    """A missing, invalid or mismatched route never falls back to an account URL."""
    root = Path(root).resolve()
    def resolve():
        try:
            path = Path(specification["path"]).resolve()
            if path.parent != root / "notification_routes" or path.suffix != ".json":
                return ""
            saved = read_json(path)
            if not isinstance(saved, dict):
                return ""
            if saved.get("run_id") != specification["run_id"]:
                return ""
            return binding_url(saved.get("binding"))
        except (OSError, ValueError, KeyError, TypeError):
            return ""
    return resolve
