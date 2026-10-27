"""Second-challenge trajectory boost for the Aliyun captcha on the sign-in page.

When the school platform's sign-in flow falls back from traceless verification
to the interactive (second) challenge, the behavioral JSON uploaded inside the
cloudauth Log3 request is the input the server-side model scores. A bare
scripted click uploads a near-empty, mechanical trajectory.

This module replays a small library of real human trajectories — perturbed,
time-anchored, and translated so the final pointer position matches the real
click — by rewriting the Log3 request at the network layer (decrypt with the
SDK's static keys, substitute the behavior JSON, re-encrypt, re-sign). The
capability and key material come from the reporter's own-scene research in
captcha-research/ (notes/02, notes/03).

Safety properties:
- automatic-click authorization expires after three seconds and is consumed
  by the first Log3 attempt, including failures; manual handoff or observed
  input outside automatic operations cancels it immediately;
- manual rounds are forwarded unchanged; only a candidate whose event window
  excludes all automatic input is archived after attendance succeeds;
- idle silent rounds with near-empty trajectories may be rewritten before
  manual handoff or an interactive challenge attempt;
- any failure degrades to pass-through, i.e. the previous behavior.

Stdlib only (pure-python AES-128-CBC) so the frozen build gains no new DLLs.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import json
import math
import random
import re
import time
import urllib.parse
import weakref
from contextlib import contextmanager
from pathlib import Path

IV = b"0123456789ABCDEF"
_KEY_UPLOAD = b"a549a55c60a39aa0"   # Log1/2/3 Data outer layer (SDK constant)
_KEY_RES = b"87f879f135f27da7"      # DeviceConfig in server responses
_DEVICE_SECRET = "DuanemHmyeE6LXCC46sJEDUw5DTlSZ"  # cloudauth RPC signing

CLOUDAUTH_URL_RE = re.compile(r"https://cloudauth-device[^/]*\.aliyuncs\.com/")
CAPTCHA_HOSTS_RE = re.compile(r"\.aliyuncs\.com|\.alicdn\.com", re.I)
_DEVICECONFIG_RE = re.compile(r"DeviceConfig[\"':=\s]+([A-Za-z0-9+/=]{40,})")
_TRAJ_MARKER = '{"mousemove"'

# 4 real human trajectories captured on the reporter's own test scene
# (gzip + base85 of the JSON list; see captcha-research/trajectories.json).
# The working library additionally grows from genuine manual completions on
# the real sign-in page (see CaptchaBoost._capture_human).
_TRAJECTORY_LIBRARY_B85 = (
    "ABzY82zK0R0{_ic%Wm5+5d0NErvxx;K9;ZM6cj}ZB<ZCFa)@(q9HtR0M~1AtTKL~f$|AXm6&vZv@If?Z#9gk2v%JdxPVz<dx"
    "U!4t$xiYwSML9ddve7Fll;|<81tjXjlhWYAmBm=oT?5N%Iw4?0ffi(DG#_JJ8?y5!rN6_oI}95Twf2m{mjZ@HuoFyGjsd;xD"
    "0<*v$`tFarJKE+}<@|W?2;vjjg{u*@vbpif5CdaNvy_QBP{R!E^V#@1F1T_j83OH}#_pxiZ$~>e6&E#;;3X{7HUmSM$ba=F@"
    "7Xbu%s&9-+!G>FUD?WyUYzrX9Xmwr>TLX$hu%*A<{X{k_S4l3!GRin5%J#E@i1m&L<v^}Nb{jx$Pz<Ro(w5>B!g3CG##a#`9"
    "-dvjJaBf-^>%k1dv$N1uWkd?*UX5Z~>UL9v&@9Sz|M-<eM_+R#GdN-|$?v+Wt&Ij2?$^u1VWlUEMw<(>p)5~x>PwE39x76Vn"
    "fEb|q7)m8ZtDB9&X=G5EGzg4GjgiE_QD|c5#K2KV)B&)_U?>VVCNff{Ot5_nr4d8)MB&uzW1zio9ZxKmiGic&RI&73fTB~Wc"
    "^^aYeGKdzIyq0OsgQaf!ytEon5l^07MOy=@dvaA)#DG8D?>i(5XzTH(v6(A9HK=*QgP@G!L``x9Uv6?{xskuC{=K>8y6Dc(O"
    "(F}-h|eN^lc)})d6XTzRCZ+b|ev>Rt`85B%O(EE61V4XnOFy#dVRcGVZPrltWlDmwtjsE~t6%C!uvD=wvOD2XTN5;&2w2+-W"
    "oI30#Uu5XdA=Fj^j46HpX_o2X9{^b3sYKC4biGJ#Hjirr5og2vakXu~qtFu2Br&Oq!rGUnheb$bYS7b1CZyg>pEk(xM*g#uh"
    "C)a7H6@Fs-pF$!EL7Awi)3JMOW4WJ$9o}=t`!~f?jb0aByc$S?!{7E{5vwZyzq%()Lf(!rv"
)


def _embedded_library() -> list[str]:
    return json.loads(gzip.decompress(base64.b85decode(_TRAJECTORY_LIBRARY_B85)))


# Where confirmed manual samples are archived (relative to the account root).
# Overridable for tests.
LIBRARY_PATH: Path | None = None
LIBRARY_MAX_SAMPLES = 40


def _library_file() -> Path:
    if LIBRARY_PATH is not None:
        return Path(LIBRARY_PATH)
    from hdusign.runtime import data_root
    return data_root() / "trajectory_library.json"


def _valid_capture(plain: str) -> bool:
    """Structural check before a human trajectory enters the library."""
    try:
        obj = json.loads(plain)
        moves = obj.get("mousemove")
        if not isinstance(moves, list) or len(moves) < 4:
            return False
        for pt in moves:
            if not {"x", "y", "t"} <= set(pt):
                return False
            if not (0 <= pt["x"] <= 4000 and 0 <= pt["y"] <= 4000
                    and 0 <= pt["t"] <= 60000):
                return False
        return bool(obj.get("pointerEvent")) and bool(obj.get("userAgent"))
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


def _append_library_file(plain: str) -> bool:
    """Archive one genuine trajectory; dedup + size-capped, never fatal."""
    try:
        path = _library_file()
        entries: list[dict] = []
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, list):
                    entries = [e for e in loaded if isinstance(e, dict)]
            except (OSError, ValueError):
                entries = []
        if any(e.get("json") == plain for e in entries):
            return False
        entries.append({"captured_at": int(time.time() * 1000), "json": plain})
        entries = entries[-LIBRARY_MAX_SAMPLES:]
        path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
        return True
    except (OSError, ValueError):
        return False


def _load_library() -> list[str]:
    """Embedded seed samples plus every genuine capture archived so far."""
    samples = _embedded_library()
    try:
        path = _library_file()
        if path.exists():
            for entry in json.loads(path.read_text(encoding="utf-8")):
                plain = entry.get("json") if isinstance(entry, dict) else None
                if isinstance(plain, str) and plain not in samples and _valid_capture(plain):
                    samples.append(plain)
    except (OSError, ValueError):
        pass
    return samples


# --------------------------------------------------------------------------
# Pure-python AES-128-CBC (FIPS-197). Used only for a few KB per challenge;
# stdlib-only keeps the frozen build free of extra OpenSSL-linked DLLs.

_SBOX = [0] * 256
_RSBOX = [0] * 256


def _build_tables() -> None:
    p = q = 1
    box: list[int] = [0] * 256
    # multiplicative inverses in GF(2^8) via generator 3
    while True:
        p = p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)
        q ^= q << 1
        q ^= q << 2
        q ^= q << 4
        q &= 0xFF
        if q & 0x80:
            q ^= 0x09
        xformed = q ^ ((q << 1) | (q >> 7)) ^ ((q << 2) | (q >> 6)) \
            ^ ((q << 3) | (q >> 5)) ^ ((q << 4) | (q >> 4))
        box[p] = (xformed ^ 0x63) & 0xFF
        if p == 1:
            break
    box[0] = 0x63
    for i in range(256):
        _SBOX[i] = box[i]
        _RSBOX[box[i]] = i


_build_tables()
_RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36]


def _xtime(a: int) -> int:
    a <<= 1
    return (a ^ 0x1B) & 0xFF if a & 0x100 else a


def _gmul(a: int, b: int) -> int:
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi:
            a ^= 0x1B
        b >>= 1
    return p


def _expand_key(key: bytes) -> list[list[int]]:
    words = [list(key[i:i + 4]) for i in range(0, 16, 4)]
    for i in range(4, 44):
        temp = list(words[i - 1])
        if i % 4 == 0:
            temp = temp[1:] + temp[:1]
            temp = [_SBOX[b] for b in temp]
            temp[0] ^= _RCON[i // 4 - 1]
        words.append([a ^ b for a, b in zip(words[i - 4], temp)])
    return [sum(words[4 * r:4 * r + 4], []) for r in range(11)]


def _encrypt_block(block: list[int], round_keys: list[list[int]]) -> list[int]:
    s = [a ^ b for a, b in zip(block, round_keys[0])]
    for rnd in range(1, 11):
        # SubBytes + ShiftRows: s'[r + 4c] = SBOX[s[r + 4*((c + r) % 4)]]
        t = [0] * 16
        for i in range(16):
            r, c = i % 4, i // 4
            t[i] = _SBOX[s[r + 4 * ((c + r) % 4)]]
        if rnd == 10:
            s = [a ^ b for a, b in zip(t, round_keys[10])]
            break
        # MixColumns
        for c in range(4):
            a0, a1, a2, a3 = t[4 * c:4 * c + 4]
            s[4 * c] = _xtime(a0) ^ _xtime(a1) ^ a1 ^ a2 ^ a3
            s[4 * c + 1] = a0 ^ _xtime(a1) ^ _xtime(a2) ^ a2 ^ a3
            s[4 * c + 2] = a0 ^ a1 ^ _xtime(a2) ^ _xtime(a3) ^ a3
            s[4 * c + 3] = _xtime(a0) ^ a0 ^ a1 ^ a2 ^ _xtime(a3)
        s = [a ^ b for a, b in zip(s, round_keys[rnd])]
    return s


_MUL9 = [_gmul(i, 9) for i in range(256)]
_MUL11 = [_gmul(i, 11) for i in range(256)]
_MUL13 = [_gmul(i, 13) for i in range(256)]
_MUL14 = [_gmul(i, 14) for i in range(256)]


def _inv_mix_columns(t: list[int]) -> list[int]:
    s = [0] * 16
    for c in range(4):
        a0, a1, a2, a3 = t[4 * c:4 * c + 4]
        s[4 * c] = _MUL14[a0] ^ _MUL11[a1] ^ _MUL13[a2] ^ _MUL9[a3]
        s[4 * c + 1] = _MUL9[a0] ^ _MUL14[a1] ^ _MUL11[a2] ^ _MUL13[a3]
        s[4 * c + 2] = _MUL13[a0] ^ _MUL9[a1] ^ _MUL14[a2] ^ _MUL11[a3]
        s[4 * c + 3] = _MUL11[a0] ^ _MUL13[a1] ^ _MUL9[a2] ^ _MUL14[a3]
    return s


def _decrypt_block(block: list[int], round_keys: list[list[int]]) -> list[int]:
    s = [a ^ b for a, b in zip(block, round_keys[10])]
    for rnd in range(9, -1, -1):
        # InvShiftRows + InvSubBytes: s'[r + 4c] = RSBOX[s[r + 4*((c - r) % 4)]]
        t = [0] * 16
        for i in range(16):
            r, c = i % 4, i // 4
            t[i] = _RSBOX[s[r + 4 * ((c - r) % 4)]]
        t = [a ^ b for a, b in zip(t, round_keys[rnd])]
        s = _inv_mix_columns(t) if rnd else t
    return s


def _cbc_encrypt(key: bytes, data: bytes) -> bytes:
    rk = _expand_key(key)
    pad = 16 - len(data) % 16
    data = data + bytes([pad]) * pad
    out = bytearray()
    prev = bytearray(IV)
    for i in range(0, len(data), 16):
        block = [a ^ b for a, b in zip(data[i:i + 16], prev)]
        prev = bytearray(_encrypt_block(block, rk))
        out += prev
    return bytes(out)


def _cbc_decrypt(key: bytes, data: bytes) -> bytes | None:
    if not data or len(data) % 16:
        return None
    rk = _expand_key(key)
    out = bytearray()
    prev = IV
    for i in range(0, len(data), 16):
        block = data[i:i + 16]
        dec = _decrypt_block(list(block), rk)
        out += bytes(a ^ b for a, b in zip(dec, prev))
        prev = block
    pad = out[-1]
    if not 1 <= pad <= 16 or out[-pad:] != bytes([pad]) * pad:
        return None
    return bytes(out[:-pad])


# --------------------------------------------------------------------------
# base64 + Log3 blob parsing (structure per captcha-research/trajlib.py)

def _b64d(text: str) -> bytes | None:
    try:
        return base64.b64decode(text + "=" * (-len(text) % 4))
    except Exception:
        return None


def _b64e(data: bytes) -> str:
    return base64.b64encode(data).decode()


_OUTER_RE = re.compile(
    r"^(?P<appkey>[0-9a-f]+)#W#(?P<flag>[^#]+)#(?P<ver>[^#]+)#CLOUD#"
    r"(?P<lens>[0-9]*(?:#[0-9]+)*)#(?P<inner_b64>[A-Za-z0-9+/=_-]+)$"
)
_B64_RUN = re.compile(r"[A-Za-z0-9+/]{16,}={0,2}")


def _parse_log_data(data_b64: str) -> dict | None:
    raw = _b64d(data_b64)
    if raw is None:
        return None
    outer = _cbc_decrypt(_KEY_UPLOAD, raw)
    if outer is None:
        return None
    try:
        outer_text = outer.decode("utf-8")
    except UnicodeDecodeError:
        return None
    m = _OUTER_RE.match(outer_text)
    if not m:
        return None
    inner_raw = _b64d(m.group("inner_b64"))
    if inner_raw is None:
        return None
    try:
        inner_text = inner_raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    head = inner_text.split("#", 1)[0]
    parts = []
    for tok in inner_text.split("#")[1:]:
        suffix = ""
        sm = re.fullmatch(r"(?P<b64>.*[A-Za-z0-9+/=])-(?P<sfx>\d+)", tok)
        if sm and len(sm.group("b64")) > 40:
            tok, suffix = sm.group("b64"), "-" + sm.group("sfx")
        part = {"raw_b64": tok, "suffix": suffix, "text": None, "segs": []}
        dec = _b64d(tok)
        if dec is None:
            parts.append(part)
            continue
        try:
            part["text"] = dec.decode("utf-8")
        except UnicodeDecodeError:
            dec = None
        parts.append(part)
        if part["text"] is None:
            continue
        for seg in part["text"].split("#"):
            hit = bool(_B64_RUN.fullmatch(seg))
            seg_raw = _b64d(seg) if hit else None
            part["segs"].append({
                "b64": seg,
                "raw": seg_raw,
                "is_aes": bool(seg_raw and len(seg_raw) % 16 == 0 and len(seg_raw) >= 16),
                "plain": None,
            })
    return {"head": outer_text[: m.end("ver")], "lens": m.group("lens"),
            "inner_head": head, "parts": parts}


def _find_trajectory(parsed: dict, session_key: bytes) -> tuple[int, int] | None:
    """Locate the (part, seg) holding the behavior JSON; decrypts in place."""
    for pi, part in enumerate(parsed["parts"]):
        if part["text"] is None:
            continue
        for si, seg in enumerate(part["segs"]):
            if not seg["is_aes"]:
                continue
            out = _cbc_decrypt(session_key, seg["raw"])
            if out is None:
                continue
            try:
                text = out.decode("utf-8")
            except UnicodeDecodeError:
                continue
            seg["plain"] = text
            if _TRAJ_MARKER in text:
                return pi, si
    return None


def _rebuild_log_data(parsed: dict, replacement: tuple[tuple[int, int], tuple[bytes, bytes]]) -> str | None:
    (pi, si), (plain, key) = replacement
    new_parts = []
    for p_i, part in enumerate(parsed["parts"]):
        if part["text"] is None or not part["segs"]:
            new_parts.append(part["raw_b64"] + part["suffix"])
            continue
        text = part["text"]
        for s_i, seg in enumerate(part["segs"]):
            if (p_i, s_i) == (pi, si):
                text = text.replace(seg["b64"], _b64e(_cbc_encrypt(key, plain)), 1)
        new_parts.append(_b64e(text.encode()) + part["suffix"])
    inner = "#".join([parsed["inner_head"]] + new_parts)
    outer = parsed["head"] + "#CLOUD#" + parsed["lens"] + "#" + _b64e(inner.encode())
    return _b64e(_cbc_encrypt(_KEY_UPLOAD, outer.encode()))


# --------------------------------------------------------------------------
# Trajectory transforms

def _perturb(traj_json: str, rng: random.Random) -> str:
    """Use one time scale for the whole round, preserving event ordering."""

    def jitter(value: int, max_delta: int) -> int:
        digits = len(str(abs(value)))
        for _ in range(24):
            cand = value + rng.randint(-max_delta, max_delta)
            if cand >= 10 and len(str(cand)) == digits:
                return cand
        return value

    obj = json.loads(traj_json)
    time_scale = rng.uniform(0.92, 1.08)
    for events in obj.values():
        if not isinstance(events, list):
            continue
        for pt in events:
            if not isinstance(pt, dict):
                continue
            for axis in ("x", "y"):
                if axis in pt:
                    pt[axis] = jitter(pt[axis], 3)
            if "t" in pt:
                pt["t"] = round(pt["t"] * time_scale)
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _anchor_timestamps(traj_json: str, now_ms: int) -> str:
    """Rewrite absolute times so the last event lands ~120 ms before send."""
    obj = json.loads(traj_json)
    old_start = int(obj.get("startTime", 0))
    if old_start <= 0:
        return traj_json
    ts = obj.get("timestamp")
    ts_offset = int(ts) - old_start if isinstance(ts, str) and ts.isdigit() else None
    max_t = 0
    for key in ("mousemove", "pointerEvent"):
        for pt in obj.get(key, []):
            if isinstance(pt.get("t"), int):
                max_t = max(max_t, pt["t"])
    delta = (now_ms - max_t - 120) - old_start
    out = traj_json.replace(str(old_start), str(old_start + delta), 1)
    if ts_offset is not None:
        out = out.replace(str(int(ts)), str(old_start + delta + ts_offset), 1)
    return out


def _translate_to_click(traj_json: str, click: tuple[float, float]) -> str:
    """Shift the path so its final pointer position equals the real click."""
    obj = json.loads(traj_json)
    moves = obj.get("mousemove", [])
    pointer = obj.get("pointerEvent") or []
    end = pointer[-1] if pointer else (moves[-1] if moves else None)
    if not end:
        return traj_json
    dx = int(round(click[0] - end.get("x", 0)))
    dy = int(round(click[1] - end.get("y", 0)))
    if dx == 0 and dy == 0:
        return traj_json
    for pt in moves:
        pt["x"] = max(0, pt["x"] + dx)
        pt["y"] = max(0, pt["y"] + dy)
    for pt in pointer:
        if "x" in pt:
            pt["x"] = max(0, pt["x"] + dx)
        if "y" in pt:
            pt["y"] = max(0, pt["y"] + dy)
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _scale_to_viewport(traj_json: str, viewport: tuple[int, int]) -> str:
    """Shrink a path whose coordinates exceed the real viewport."""
    obj = json.loads(traj_json)
    xs = [p["x"] for p in obj.get("mousemove", [])] + [p["x"] for p in obj.get("pointerEvent", []) if "x" in p]
    ys = [p["y"] for p in obj.get("mousemove", [])] + [p["y"] for p in obj.get("pointerEvent", []) if "y" in p]
    if not xs or not ys:
        return traj_json
    vw, vh = viewport
    factor = min((vw - 20) / max(1, max(xs)), (vh - 20) / max(1, max(ys)), 1.0)
    if factor >= 1.0:
        return traj_json
    for key in ("mousemove", "pointerEvent"):
        for pt in obj.get(key, []):
            if "x" in pt:
                pt["x"] = int(round(pt["x"] * factor))
            if "y" in pt:
                pt["y"] = int(round(pt["y"] * factor))
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _place_in_viewport(traj_json: str, target: tuple[float, float] | None,
                       viewport: tuple[int, int]) -> str:
    """Fit around the final click, keeping both the endpoint and every point in bounds."""
    obj = json.loads(traj_json)
    moves = obj.get("mousemove", [])
    pointer = obj.get("pointerEvent", [])
    points = [p for events in obj.values() if isinstance(events, list)
              for p in events if isinstance(p, dict) and "x" in p and "y" in p]
    if not points:
        return traj_json
    end = pointer[-1] if pointer else moves[-1]
    xmax, ymax = viewport[0] - 1, viewport[1] - 1
    tx, ty = target if target is not None else (end["x"], end["y"])
    tx, ty = min(max(round(tx), 0), xmax), min(max(round(ty), 0), ymax)
    origin_x, origin_y = end["x"], end["y"]
    scale = 1.0
    for pt in points:
        for delta, anchor, limit in ((pt["x"] - origin_x, tx, xmax),
                                      (pt["y"] - origin_y, ty, ymax)):
            if delta > 0:
                scale = min(scale, (limit - anchor) / delta)
            elif delta < 0:
                scale = min(scale, anchor / -delta)
    for pt in points:
        pt["x"] = min(xmax, max(0, round(tx + (pt["x"] - origin_x) * scale)))
        pt["y"] = min(ymax, max(0, round(ty + (pt["y"] - origin_y) * scale)))
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _min_jerk(u: float) -> float:
    """Minimum-jerk position profile s(u), the standard human pointing model."""
    return 10 * u ** 3 - 15 * u ** 4 + 6 * u ** 5


def _synthesize_movement(rng: random.Random, target: tuple[float, float],
                         viewport: tuple[int, int] = (1280, 720)) -> str:
    """Generate one human-like pointing movement at the given target.

    Two-phase submovements along a bowed quadratic Bézier with a minimum-jerk
    velocity profile, per-point tremor, occasional stationary hover points,
    and the exact field shapes of the captured samples (startTime int,
    timestamp str). Timestamps are placeholders — the caller anchors them.
    """
    vw, vh = viewport
    dist = rng.uniform(70, 220)
    ang = rng.uniform(0, 2 * math.pi)
    sx = max(5.0, min(target[0] + dist * math.cos(ang), vw - 5))
    sy = max(5.0, min(target[1] + dist * math.sin(ang), vh - 5))
    bow = rng.uniform(-0.25, 0.25) * dist
    mx = (sx + target[0]) / 2 - bow * math.sin(ang)
    my = (sy + target[1]) / 2 + bow * math.cos(ang)

    n = rng.randint(8, 16)
    duration = rng.uniform(350, 1300)
    moves: list[dict] = []
    last_t = 0
    for i in range(1, n + 1):
        u = i / n
        s = _min_jerk(u)
        x = (1 - s) ** 2 * sx + 2 * (1 - s) * s * mx + s ** 2 * target[0]
        y = (1 - s) ** 2 * sy + 2 * (1 - s) * s * my + s ** 2 * target[1]
        x += rng.uniform(-1.5, 1.5)
        y += rng.uniform(-1.5, 1.5)
        t = int(round(duration * u + rng.uniform(-8, 8)))
        t = max(t, last_t + rng.randint(12, 30))
        last_t = t
        moves.append({"x": int(round(x)), "y": int(round(y)), "t": t})
        # occasional stationary hover, like the real samples
        if rng.random() < 0.12 and i < n:
            last_t += rng.randint(25, 60)
            moves.append({"x": moves[-1]["x"], "y": moves[-1]["y"], "t": last_t})
    final = moves[-1]
    final["x"], final["y"] = int(round(target[0])), int(round(target[1]))
    pointer = {"p": 0.5, "b": 1, "pt": 1, "w": 1, "h": 1,
               "x": final["x"], "y": final["y"], "it": True, "t": final["t"]}
    return json.dumps({
        "mousemove": moves, "mouseclick": [], "keyup": [],
        "scrollTop": [], "scrollLeft": [], "pointerEvent": [pointer],
        "clientType": "desktop",
        "startTime": 1790000000000,
        "timestamp": "1789999988500",
        "userAgent": "",
    }, separators=(",", ":"), ensure_ascii=False)


# --------------------------------------------------------------------------
# RPC re-signing (cloudauth channel)

def _percent_encode(value: str) -> str:
    return urllib.parse.quote(str(value), safe="-_.~")


def _sign_params(params: dict) -> dict:
    out = {k: v for k, v in params.items() if k != "Signature"}
    canonical = "&".join(f"{_percent_encode(k)}={_percent_encode(v)}"
                         for k, v in sorted(out.items()))
    string_to_sign = "POST&%2F&" + _percent_encode(canonical)
    digest = hmac.new((_DEVICE_SECRET + "&").encode(), string_to_sign.encode(),
                      hashlib.sha1).digest()
    out["Signature"] = base64.b64encode(digest).decode()
    return out


# --------------------------------------------------------------------------
# Injector

_INPUT_BINDING = "__echoSignInputObserved"
_INPUT_SCRIPT = """(() => {
    if (window.__echoSignInputInstalled) return;
    window.__echoSignInputInstalled = true;
    window.__echoSignAutomaticInput = false;
    for (const name of ['pointermove', 'pointerdown', 'keydown', 'touchstart']) {
        addEventListener(name, event => {
            if (!event.isTrusted) return;
            window.__echoSignInputObserved({at: Date.now(),
                automatic: window.__echoSignAutomaticInput}).catch(() => {});
        }, {capture: true, passive: true});
    }
})();"""

class CaptchaBoost:
    """Network-layer behavior replay for the Aliyun captcha.

    Automatic-click authorization is bounded and single-use. Observed manual
    input and explicit handoff both suppress all rewriting. A rich trajectory
    is only a capture candidate when its entire event window follows observed
    manual input and the last automatic input; attendance success commits it.
    Uncertain provenance and processing failures pass the request unchanged.
    """

    SPARSE_POINT_LIMIT = 3
    ARM_SECONDS = 3.0

    def __init__(self, page) -> None:
        self.page = page
        self._session_keys: list[bytes] = []
        self._armed = False
        self._armed_until = 0.0
        self._challenge_attempted = False
        self._manual_mode = False
        self._manual_input_at: int | None = None
        self._last_automatic_at = 0
        self._automatic_depth = 0
        self._observing_inputs = False
        self._pending_manual: str | None = None
        self.click_point: tuple[float, float] | None = None
        self._viewport: tuple[int, int] | None = None
        self._injected_hashes: set[str] = set()
        self.captures = 0
        self.rewrites = 0
        self.traceless_rewrites = 0
        self.errors = 0

    # -- lifecycle ---------------------------------------------------------

    def install(self) -> None:
        try:
            self.page.expose_binding(_INPUT_BINDING, self._on_input)
            self.page.add_init_script(_INPUT_SCRIPT)
            for frame in self.page.frames:
                frame.evaluate(_INPUT_SCRIPT)
            self._observing_inputs = True
        except Exception:
            # Without input provenance, pass requests through and never archive.
            self._observing_inputs = False
        self.page.on("response", self._on_response)
        self.page.route(CLOUDAUTH_URL_RE, self._on_route)
        self._refresh_viewport()

    def _refresh_viewport(self) -> None:
        try:
            size = self.page.evaluate("[innerWidth, innerHeight]")
            if (isinstance(size, list) and len(size) == 2
                    and all(isinstance(v, int) and v > 100 for v in size)):
                self._viewport = (size[0], size[1])
        except Exception:
            pass

    def arm(self, click_point: tuple[float, float]) -> None:
        if self._manual_mode or not self._observing_inputs:
            return
        self.click_point = click_point
        self._armed = True
        self._armed_until = time.monotonic() + self.ARM_SECONDS
        self._challenge_attempted = True

    def begin_round(self) -> None:
        self._armed = False
        self._challenge_attempted = False
        self._manual_mode = False
        self._manual_input_at = None
        self._pending_manual = None

    def manual_takeover(self) -> None:
        self._armed = False
        self._manual_mode = True

    def _on_input(self, source, event) -> None:
        if not isinstance(event, dict) or not isinstance(event.get("at"), (int, float)):
            return
        when = int(event["at"])
        if event.get("automatic") or self._automatic_depth:
            self._last_automatic_at = max(self._last_automatic_at, when)
            self._pending_manual = None
        else:
            self.manual_takeover()
            if self._manual_input_at is None:
                self._manual_input_at = when

    def finish_round(self, success: bool) -> None:
        self._armed = False
        plain, self._pending_manual = self._pending_manual, None
        if success and plain is not None:
            self._capture_human(plain)

    # -- hooks -------------------------------------------------------------

    def _on_response(self, response) -> None:
        try:
            if not CAPTCHA_HOSTS_RE.search(response.url):
                return
            body = response.text()
        except Exception:
            return
        for match in _DEVICECONFIG_RE.finditer(body):
            plain = _cbc_decrypt(_KEY_RES, _b64d(match.group(1)) or b"")
            if plain is None:
                continue
            parts = plain.decode("utf-8", errors="ignore").split("#")
            if len(parts) < 4:
                continue
            key = _b64d(parts[0]) or b""
            if len(key) == 16 and key not in self._session_keys:
                self._session_keys.append(key)

    def _on_route(self, route, request) -> None:
        try:
            post = request.post_data or ""
            params = dict(urllib.parse.parse_qsl(post, keep_blank_values=True))
            if params.get("Action") == "Log3":
                body = self._rewrite_body(params)
                if body is not None:
                    route.continue_(post_data=body)
                    return
            route.continue_()
        except Exception:
            self.errors += 1
            route.continue_()

    # -- rewrite -----------------------------------------------------------

    def _rewrite_body(self, params: dict) -> str | None:
        armed = self._armed and time.monotonic() < self._armed_until
        # Consume the next Log3 attempt even if it cannot be parsed or rewritten.
        self._armed = False
        if not self._observing_inputs:
            return None
        data_b64 = params.get("Data", "")
        parsed = _parse_log_data(data_b64)
        if parsed is None:
            return None
        loc = None
        used_key = None
        for key in reversed(self._session_keys):
            loc = _find_trajectory(parsed, key)
            if loc is not None:
                used_key = key
                break
        if loc is None or used_key is None:
            return None
        seg = parsed["parts"][loc[0]]["segs"][loc[1]]
        try:
            current = json.loads(seg["plain"])
            points = len(current.get("mousemove", []))
        except (ValueError, TypeError):
            return None
        if self._manual_mode:
            self._queue_manual(seg["plain"])
            return None
        if armed:
            target = self.click_point
        elif points <= self.SPARSE_POINT_LIMIT and not self._challenge_attempted:
            # silent round: no real click happened — anchor the injected path
            # at the pointer's tracked position so the movement stream stays
            # continuous with what the session really showed so far
            target = pointer_position(self.page)
        else:
            # Point count alone cannot distinguish automatic from manual input.
            return None
        injected = self._build_injection(current, target)
        if injected is None or self._manual_mode or not self._observing_inputs:
            return None
        new_data = _rebuild_log_data(parsed, (loc, (injected.encode(), used_key)))
        if new_data is None:
            return None
        self._injected_hashes.add(hashlib.sha256(injected.encode()).hexdigest())
        if armed:
            self.rewrites += 1
            print("[i] 二次挑战已注入扰动后的真人轨迹样本", flush=True)
        else:
            self.traceless_rewrites += 1
            print("[i] 无痕评分轮已注入扰动后的真人轨迹样本", flush=True)
        return urllib.parse.urlencode(_sign_params({**params, "Data": new_data}))

    def _build_injection(self, current: dict, target: tuple[float, float] | None) -> str | None:
        """Assemble the replacement trajectory from the library or synthesizer.

        The current session's clientType/userAgent are kept so the injected
        JSON stays consistent with the fingerprint uploaded in Log2.
        """
        rng = random.Random(random.SystemRandom().randrange(1 << 32))
        library = _load_library()
        self._refresh_viewport()
        viewport = self._viewport or (1280, 720)
        try:
            source = rng.choice(library + [None])
            if source is None:
                vw, vh = viewport
                if target is None:
                    target = (rng.uniform(vw * 0.3, vw * 0.7),
                              rng.uniform(vh * 0.3, vh * 0.7))
                traj = _synthesize_movement(rng, target, (vw, vh))
            else:
                traj = _perturb(source, rng)
            traj = _place_in_viewport(traj, target, viewport)
        except (ValueError, KeyError, IndexError):
            return None
        obj = json.loads(traj)
        obj["clientType"] = current.get("clientType", "desktop")
        if current.get("userAgent"):
            obj["userAgent"] = current["userAgent"]
        traj = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
        return _anchor_timestamps(traj, int(time.time() * 1000))


    def _queue_manual(self, plain: str) -> None:
        """Keep only a wholly manual window; commit after attendance succeeds."""
        if self._manual_input_at is None or self._automatic_depth or not _valid_capture(plain):
            return
        if hashlib.sha256(plain.encode()).hexdigest() in self._injected_hashes:
            return
        try:
            obj = json.loads(plain)
            events = [event for value in obj.values() if isinstance(value, list)
                      for event in value]
            if not events or any(not isinstance(event, dict)
                                 or not isinstance(event.get("t"), (int, float))
                                 for event in events):
                return
            first = int(obj["startTime"]) + min(event["t"] for event in events)
            if first <= self._last_automatic_at or first < self._manual_input_at:
                return
        except (ValueError, TypeError, KeyError):
            return
        self._pending_manual = plain

    def _capture_human(self, plain: str) -> None:
        digest = hashlib.sha256(plain.encode()).hexdigest()
        if digest in self._injected_hashes or not _valid_capture(plain):
            return
        if _append_library_file(plain):
            self.captures += 1
            print("[i] 已归档一份真人轨迹到本地样本库", flush=True)


_boosts: "weakref.WeakKeyDictionary[object, CaptchaBoost]" = weakref.WeakKeyDictionary()


def install_for(page, *, enabled: bool = True) -> None:
    """Attach a boost to a sign-in page; no-op when disabled."""
    if not enabled or page in _boosts:
        return
    boost = CaptchaBoost(page)
    _boosts[page] = boost
    boost.install()


def note_click_for(page, x: float, y: float) -> None:
    """Arm the boost from the automated verification click position."""
    boost = _boosts.get(page)
    if boost is not None:
        boost.arm((x, y))


def enabled_for(page) -> bool:
    """Whether behavior boosting is active on this page."""
    return page in _boosts


def manual_for(page) -> bool:
    boost = _boosts.get(page)
    return boost is not None and boost._manual_mode


def begin_round_for(page) -> None:
    boost = _boosts.get(page)
    if boost is not None:
        boost.begin_round()


def manual_takeover_for(page) -> None:
    boost = _boosts.get(page)
    if boost is not None:
        boost.manual_takeover()


def finish_round_for(page, *, success: bool = False) -> None:
    boost = _boosts.get(page)
    if boost is not None:
        boost.finish_round(success)


@contextmanager
def automatic_input(page):
    """Mark only our own input operations; unmarked trusted input hands control over."""
    boost = _boosts.get(page)
    if boost is None:
        yield
        return
    boost._automatic_depth += 1
    boost._last_automatic_at = max(boost._last_automatic_at, int(time.time() * 1000))
    boost._pending_manual = None
    try:
        try:
            for frame in page.frames:
                frame.evaluate("value => { window.__echoSignAutomaticInput = value; }", True)
        except Exception:
            boost._observing_inputs = False
        yield
    finally:
        try:
            # The evaluation also drains pending binding callbacks before we
            # leave the automatic interval.
            for frame in page.frames:
                frame.evaluate("value => { window.__echoSignAutomaticInput = value; }",
                               boost._automatic_depth > 1)
        except Exception:
            boost._observing_inputs = False
        boost._last_automatic_at = max(boost._last_automatic_at, int(time.time() * 1000))
        boost._automatic_depth -= 1


# -- pointer tracking -------------------------------------------------------
# The virtual pointer's last known position per page. Keeping every move
# continuous from the previous position (wander -> digit keys -> submit)
# removes the teleport gaps that a behavior collector would see between
# fragmented approach bursts.

_pointer_positions: "weakref.WeakKeyDictionary[object, tuple[float, float]]" = (
    weakref.WeakKeyDictionary())


def note_pointer(page, x: float, y: float) -> None:
    _pointer_positions[page] = (float(x), float(y))


def pointer_position(page) -> tuple[float, float] | None:
    return _pointer_positions.get(page)
