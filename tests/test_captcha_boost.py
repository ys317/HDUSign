"""Unit tests for hdusign.captcha_boost (second-challenge trajectory replay).

The Log3 blob used here is synthetic — built with the module's own AES from
the same nested structure as real captures — so the tests stay hermetic.
Every library lives in a temporary directory; no real captures are read.
"""
from __future__ import annotations

import base64
import json
import os
import random
import sys
import time
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hdusign import captcha_boost as cb

SESSION_KEY = b"0123456789abcdef"
TRAJ = json.dumps({
    "mousemove": [{"x": 372, "y": 222, "t": 495}, {"x": 358, "y": 217, "t": 579},
                  {"x": 352, "y": 213, "t": 670}, {"x": 317, "y": 234, "t": 736}],
    "mouseclick": [], "keyup": [], "scrollTop": [], "scrollLeft": [],
    "pointerEvent": [{"p": 0.5, "b": 1, "pt": 1, "w": 1, "h": 1, "x": 352,
                      "y": 211, "it": True, "t": 722}],
    "clientType": "desktop", "startTime": "1792667030129",
    "timestamp": "1792667028649", "userAgent": "Mozilla/5.0 test",
}, separators=(",", ":"))


def build_synthetic_log3(traj_json: str = TRAJ, session_id: str = "ab" + "0" * 30) -> str:
    """Assemble a structure-equivalent Log3 Data blob with the module's AES."""
    sid = f"{session_id}-h-1792742239614-{'0' * 32}"

    def seg(plain: str) -> str:
        return cb._b64e(cb._cbc_encrypt(SESSION_KEY, plain.encode()))

    part1 = "#".join([sid, seg("0#0#1#0#0"), seg("saf-captcha"), seg("1792667030955")])
    part2 = "#".join([sid, seg(traj_json), seg("1792667030956")])
    inner = "#".join(["511", cb._b64e(part1.encode()) + "-504", cb._b64e(part2.encode())])
    outer = (f"ab{'0' * 30}#W#{'A' * 44}#W20220202#CLOUD#616#"
             f"{cb._b64e(inner.encode())}")
    return cb._b64e(cb._cbc_encrypt(cb._KEY_UPLOAD, outer.encode()))


class FakeRoute:
    def __init__(self):
        self.calls: list[dict] = []

    def continue_(self, post_data: str | None = None):
        self.calls.append({"post_data": post_data})


class FakeRequest:
    def __init__(self, url: str, post_data: str):
        self.url = url
        self.post_data = post_data


class FakeResponse:
    def __init__(self, url: str, body: str):
        self.url = url
        self._body = body

    def text(self):
        return self._body


class FakePage:
    def __init__(self):
        self.handlers: dict[str, list] = {}
        self.routes: list = []
        self.frames = [self]
        self.bindings = {}

    def expose_binding(self, name, callback):
        self.bindings[name] = callback

    def add_init_script(self, script):
        pass

    def evaluate(self, script, arg=None):
        if script == "[innerWidth, innerHeight]":
            return [1280, 720]

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def route(self, pattern, handler):
        self.routes.append((pattern, handler))


class IsolatedLibraryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.library_path = Path(self._tmp.name) / "lib.json"
        self.library_patch = patch.object(cb, "LIBRARY_PATH", self.library_path)
        self.library_patch.start()
        self.addCleanup(self.library_patch.stop)
        self.addCleanup(cb._boosts.clear)
        self.addCleanup(cb._pointer_positions.clear)


class AesTests(IsolatedLibraryTests):
    def test_fips197_vector(self):
        rk = cb._expand_key(bytes(range(16)))
        ct = bytes(cb._encrypt_block(
            list(bytes.fromhex("00112233445566778899aabbccddeeff")), rk))
        self.assertEqual(ct.hex(), "69c4e0d86a7b0430d8cdb78070b4c55a")

    def test_cbc_roundtrip(self):
        data = bytes(range(256)) * 4
        key = b"a549a55c60a39aa0"
        self.assertEqual(cb._cbc_decrypt(key, cb._cbc_encrypt(key, data)), data)

    def test_decrypt_rejects_garbage(self):
        self.assertIsNone(cb._cbc_decrypt(b"a549a55c60a39aa0", b"\x00" * 32))


class BlobTests(IsolatedLibraryTests):
    def test_parse_and_identity_rebuild(self):
        data = build_synthetic_log3()
        parsed = cb._parse_log_data(data)
        self.assertIsNotNone(parsed)
        loc = cb._find_trajectory(parsed, SESSION_KEY)
        self.assertIsNotNone(loc)
        seg = parsed["parts"][loc[0]]["segs"][loc[1]]
        rebuilt = cb._rebuild_log_data(
            parsed, (loc, (seg["plain"].encode(), SESSION_KEY)))
        self.assertEqual(rebuilt, data)

    def test_replacement_roundtrip(self):
        data = build_synthetic_log3()
        parsed = cb._parse_log_data(data)
        loc = cb._find_trajectory(parsed, SESSION_KEY)
        new_traj = TRAJ.replace("1792667030129", "1792999999999")
        rebuilt = cb._rebuild_log_data(
            parsed, (loc, (new_traj.encode(), SESSION_KEY)))
        parsed2 = cb._parse_log_data(rebuilt)
        loc2 = cb._find_trajectory(parsed2, SESSION_KEY)
        self.assertEqual(parsed2["parts"][loc2[0]]["segs"][loc2[1]]["plain"], new_traj)

class TransformTests(IsolatedLibraryTests):
    def test_perturb_keeps_structure(self):
        rng = random.Random(7)
        out = cb._perturb(TRAJ, rng)
        obj = json.loads(out)
        self.assertEqual(len(obj["mousemove"]), 4)
        self.assertEqual(obj["startTime"], "1792667030129")
        self.assertEqual(len(out), len(TRAJ))  # digit-preserving jitter
        self.assertNotEqual(out, TRAJ)

    def test_time_warp_preserves_order_across_event_streams(self):
        mixed = json.loads(TRAJ)
        mixed["keyup"] = [{"t": 721}, {"t": 723}]
        mixed["mouseclick"] = [{"x": 352, "y": 211, "t": 722}]
        for source in cb._embedded_library() + [json.dumps(mixed)]:
            before = json.loads(source)
            for seed in range(20):
                after = json.loads(cb._perturb(source, random.Random(seed)))
                pairs = [(old["t"], new["t"])
                         for field in ("mousemove", "pointerEvent", "keyup", "mouseclick")
                         for old, new in zip(before[field], after[field])]
                transformed = [new for _, new in sorted(pairs)]
                self.assertEqual(transformed, sorted(transformed))

    def test_placement_keeps_edge_click_and_every_point_inside_viewport(self):
        source = json.loads(TRAJ)
        source["mouseclick"] = [{"x": 1500, "y": 800, "t": 700}]
        for viewport in ((1280, 720), (320, 240)):
            width, height = viewport
            for target in ((0, 0), (width - 1, height - 1),
                           (width - 10, height - 20), (2, height - 2)):
                result = json.loads(cb._place_in_viewport(json.dumps(source), target, viewport))
                self.assertEqual(tuple(result["pointerEvent"][-1][k] for k in ("x", "y")), target)
                for point in result["mousemove"] + result["pointerEvent"] + result["mouseclick"]:
                    self.assertTrue(0 <= point["x"] < width)
                    self.assertTrue(0 <= point["y"] < height)

    def test_anchor_lands_before_now(self):
        now = int(time.time() * 1000)
        out = cb._anchor_timestamps(TRAJ, now)
        obj = json.loads(out)
        max_t = max(p["t"] for p in obj["mousemove"] + obj["pointerEvent"])
        self.assertLess(int(obj["startTime"]) + max_t, now)
        self.assertGreater(int(obj["startTime"]) + max_t, now - 2000)
        # timestamp keeps its original offset from startTime
        orig = json.loads(TRAJ)
        self.assertEqual(int(obj["timestamp"]) - int(obj["startTime"]),
                         int(orig["timestamp"]) - int(orig["startTime"]))

    def test_translate_to_click(self):
        out = cb._translate_to_click(TRAJ, (500.0, 300.0))
        obj = json.loads(out)
        self.assertEqual((obj["pointerEvent"][0]["x"], obj["pointerEvent"][0]["y"]), (500, 300))
        self.assertEqual(obj["mousemove"][-1]["x"], 317 + (500 - 352))


class InjectorTests(IsolatedLibraryTests):
    def _make_page(self) -> tuple[FakePage, cb.CaptchaBoost]:
        cb._boosts.clear()
        page = FakePage()
        cb.install_for(page)
        boost = cb._boosts[page]
        return page, boost

    def _log3_request(self, data: str) -> tuple[FakeRoute, FakeRequest]:
        params = {"Action": "Log3", "Data": data, "SignatureNonce": "n",
                  "Signature": "old"}
        return FakeRoute(), FakeRequest(
            "https://cloudauth-device-dualstack.cn-shanghai.aliyuncs.com/",
            urllib.parse.urlencode(params))
    def test_rewrite_when_armed(self):
        page, boost = self._make_page()
        boost._session_keys = [SESSION_KEY]
        cb.note_click_for(page, 352.0, 211.0)
        data = build_synthetic_log3()
        route, request = self._log3_request(data)
        page.routes[0][1](route, request)
        self.assertEqual(len(route.calls), 1)
        body = route.calls[0]["post_data"]
        self.assertIsNotNone(body)
        new_params = dict(urllib.parse.parse_qsl(body, keep_blank_values=True))
        self.assertNotEqual(new_params["Data"], data)
        self.assertNotEqual(new_params["Signature"], "old")
        # decrypt the rewritten blob and verify the injected trajectory
        parsed = cb._parse_log_data(new_params["Data"])
        loc = cb._find_trajectory(parsed, SESSION_KEY)
        injected = json.loads(parsed["parts"][loc[0]]["segs"][loc[1]]["plain"])
        self.assertIn("mousemove", injected)
        self.assertEqual(boost.rewrites, 1)
        # single-shot: a second armed Log3 with a rich trajectory passes
        # through untouched (the human/automation round has already run)
        route2, request2 = self._log3_request(data)
        page.routes[0][1](route2, request2)
        self.assertIsNone(route2.calls[0]["post_data"])

    def test_sparse_traceless_round_is_injected(self):
        page, boost = self._make_page()
        boost._session_keys = [SESSION_KEY]
        sparse = json.loads(TRAJ)
        sparse["mousemove"] = sparse["mousemove"][:2]  # idle/silent round
        data = build_synthetic_log3(json.dumps(sparse, separators=(",", ":")))
        route, request = self._log3_request(data)
        page.routes[0][1](route, request)
        body = route.calls[0]["post_data"]
        self.assertIsNotNone(body)
        new_params = dict(urllib.parse.parse_qsl(body, keep_blank_values=True))
        self.assertNotEqual(new_params["Data"], data)
        parsed = cb._parse_log_data(new_params["Data"])
        loc = cb._find_trajectory(parsed, SESSION_KEY)
        injected = json.loads(parsed["parts"][loc[0]]["segs"][loc[1]]["plain"])
        self.assertGreater(len(injected["mousemove"]), 3)
        self.assertEqual(boost.traceless_rewrites, 1)
        self.assertEqual(boost.rewrites, 0)

    def test_rich_round_untouched_when_not_armed(self):
        page, boost = self._make_page()
        boost._session_keys = [SESSION_KEY]
        route, request = self._log3_request(build_synthetic_log3())
        page.routes[0][1](route, request)
        self.assertIsNone(route.calls[0]["post_data"])

    def test_pass_through_without_session_key(self):
        page, boost = self._make_page()
        cb.note_click_for(page, 352.0, 211.0)
        route, request = self._log3_request(build_synthetic_log3())
        page.routes[0][1](route, request)
        self.assertIsNone(route.calls[0]["post_data"])
        self.assertFalse(boost._armed)

    def test_failed_attempt_does_not_rewrite_a_later_round(self):
        page, boost = self._make_page()
        boost._session_keys = [SESSION_KEY]
        cb.note_click_for(page, 352.0, 211.0)
        route, request = self._log3_request("unparseable")
        boost._on_route(route, request)
        self.assertFalse(boost._armed)
        sparse = json.loads(TRAJ)
        sparse["mousemove"] = sparse["mousemove"][:1]
        for plain in (TRAJ, json.dumps(sparse)):
            route, request = self._log3_request(build_synthetic_log3(plain))
            boost._on_route(route, request)
            self.assertIsNone(route.calls[0]["post_data"])

    def test_expired_arm_is_not_used(self):
        page, boost = self._make_page()
        boost._session_keys = [SESSION_KEY]
        cb.note_click_for(page, 352.0, 211.0)
        with patch.object(cb.time, "monotonic", return_value=boost._armed_until + 1):
            route, request = self._log3_request(build_synthetic_log3())
            boost._on_route(route, request)
        self.assertIsNone(route.calls[0]["post_data"])
        self.assertFalse(boost._armed)

    def test_manual_takeover_protects_sparse_and_rich_rounds(self):
        for point_count in (0, 1, 4):
            page, boost = self._make_page()
            boost._session_keys = [SESSION_KEY]
            cb.note_click_for(page, 352.0, 211.0)
            cb.manual_takeover_for(page)
            plain = json.loads(TRAJ)
            plain["mousemove"] = plain["mousemove"][:point_count]
            route, request = self._log3_request(build_synthetic_log3(json.dumps(plain)))
            boost._on_route(route, request)
            self.assertIsNone(route.calls[0]["post_data"])
            self.assertEqual(boost.captures, 0)

    def test_observed_manual_input_cancels_arm_before_prompt(self):
        page, boost = self._make_page()
        cb.note_click_for(page, 352.0, 211.0)
        page.bindings[cb._INPUT_BINDING]({}, {"at": 1000, "automatic": False})
        self.assertTrue(cb.manual_for(page))
        self.assertFalse(boost._armed)

    def test_input_arriving_during_preparation_still_prevents_rewrite(self):
        page, boost = self._make_page()
        boost._session_keys = [SESSION_KEY]
        cb.note_click_for(page, 352, 211)
        def prepare(current, target):
            boost._on_input({}, {"at": int(time.time() * 1000), "automatic": False})
            return TRAJ
        with patch.object(boost, "_build_injection", side_effect=prepare):
            self.assertIsNone(boost._rewrite_body({"Data": build_synthetic_log3()}))
        self.assertEqual(boost.rewrites, 0)

    def test_empty_log3_consumes_authorization(self):
        page, boost = self._make_page()
        cb.note_click_for(page, 352, 211)
        route, request = self._log3_request("")
        boost._on_route(route, request)
        self.assertFalse(boost._armed)
        self.assertIsNone(route.calls[0]["post_data"])

    def test_observer_failure_passes_through_without_library_access(self):
        page = FakePage()
        page.expose_binding = lambda *args: (_ for _ in ()).throw(RuntimeError("unavailable"))
        cb.install_for(page)
        boost = cb._boosts[page]
        boost._session_keys = [SESSION_KEY]
        cb.note_click_for(page, 352, 211)
        with patch.object(cb, "_load_library", side_effect=AssertionError("unexpected read")):
            self.assertIsNone(boost._rewrite_body({"Data": build_synthetic_log3()}))

    def test_session_key_from_device_config(self):
        page, boost = self._make_page()
        cfg_inner = "#".join([cb._b64e(SESSION_KEY), cb._b64e(b"1"),
                               "sid", "1.5.2/feilin056", ""])
        cfg_body = json.dumps({"ResultObject": {
            "DeviceConfig": cb._b64e(cb._cbc_encrypt(cb._KEY_RES, cfg_inner.encode()))}})
        boost._on_response(FakeResponse(
            "https://cloudauth-device.cn-shanghai.aliyuncs.com/", cfg_body))
        self.assertEqual(boost._session_keys, [SESSION_KEY])

    def test_disabled_install(self):
        cb._boosts.clear()
        page = FakePage()
        cb.install_for(page, enabled=False)
        self.assertNotIn(page, cb._boosts)
        self.assertFalse(cb.enabled_for(page))


class IdleBehaviorTests(IsolatedLibraryTests):
    """browser.py helpers: pointer wander / approach paths, never fatal."""

    class WanderPage:
        def __init__(self):
            self.moves: list[tuple[float, float]] = []

        def evaluate(self, _expr):
            return 1280

        @property
        def mouse(self):
            outer = self

            class Mouse:
                def move(self, x, y):
                    outer.moves.append((x, y))

            return Mouse()

        def wait_for_timeout(self, ms):
            pass

    def test_idle_wander_moves_without_clicks(self):
        from hdusign import browser
        page = self.WanderPage()
        browser._idle_wander(page)
        self.assertGreaterEqual(len(page.moves), 2)
        for x, y in page.moves:
            self.assertTrue(0 <= x <= 1280)
            self.assertGreaterEqual(y, 0)

    def test_approach_pointer_noop_when_disabled(self):
        from hdusign import browser
        cb._boosts.clear()
        page = self.WanderPage()
        browser._approach_pointer(page, None)  # must not raise or move
        self.assertEqual(page.moves, [])

    def test_pointer_path_is_continuous(self):
        """Consecutive glides leave no teleport gaps in the move stream."""
        import math
        from hdusign import browser
        cb._pointer_positions.clear()
        page = self.WanderPage()
        browser._pointer_move_to(page, 400.0, 300.0)
        n_first = len(page.moves)
        self.assertGreaterEqual(n_first, 2)
        self.assertEqual(cb.pointer_position(page), (400.0, 300.0))
        browser._pointer_move_to(page, 120.0, 640.0)
        self.assertEqual(cb.pointer_position(page), (120.0, 640.0))
        # every consecutive move pair is a glide step, never a teleport
        for (x1, y1), (x2, y2) in zip(page.moves, page.moves[1:]):
            self.assertLessEqual(math.hypot(x2 - x1, y2 - y1), 130)

    def test_sparse_round_anchors_at_tracked_pointer(self):
        cb._boosts.clear()
        page = FakePage()
        cb.install_for(page)
        boost = cb._boosts[page]
        boost._session_keys = [SESSION_KEY]
        cb.note_pointer(page, 555.0, 333.0)
        sparse = json.loads(TRAJ)
        sparse["mousemove"] = sparse["mousemove"][:2]
        data = build_synthetic_log3(json.dumps(sparse, separators=(",", ":")))
        route, request = FakeRoute(), FakeRequest(
            "https://cloudauth-device.cn-shanghai.aliyuncs.com/",
            urllib.parse.urlencode({"Action": "Log3", "Data": data}))
        page.routes[0][1](route, request)
        body = route.calls[0]["post_data"]
        self.assertIsNotNone(body)
        new_data = dict(urllib.parse.parse_qsl(body, keep_blank_values=True))["Data"]
        parsed = cb._parse_log_data(new_data)
        loc = cb._find_trajectory(parsed, SESSION_KEY)
        injected = json.loads(parsed["parts"][loc[0]]["segs"][loc[1]]["plain"])
        end = (injected["pointerEvent"][-1]["x"], injected["pointerEvent"][-1]["y"])
        self.assertEqual(end, (555, 333))


class SynthesisTests(IsolatedLibraryTests):
    def test_structure_and_types(self):
        out = cb._synthesize_movement(random.Random(42), (352.0, 211.0))
        obj = json.loads(out)
        self.assertIsInstance(obj["startTime"], int)
        self.assertIsInstance(obj["timestamp"], str)
        self.assertGreaterEqual(len(obj["mousemove"]), 8)
        end = obj["pointerEvent"][-1]
        self.assertEqual((end["x"], end["y"]), (352, 211))
        self.assertTrue(end["it"] is True and end["p"] == 0.5 and end["b"] == 1)
        ts = [p["t"] for p in obj["mousemove"]]
        self.assertEqual(ts, sorted(ts))
        self.assertTrue(all(b - a >= 10 for a, b in zip(ts, ts[1:])))
        self.assertLess(max(ts), 3000)

    def test_deterministic_with_seed(self):
        a = cb._synthesize_movement(random.Random(7), (100.0, 100.0))
        b = cb._synthesize_movement(random.Random(7), (100.0, 100.0))
        self.assertEqual(a, b)

    def test_viewport_scaling(self):
        big = build_synthetic_log3()  # sample coordinates up to ~370px
        parsed = cb._parse_log_data(big)
        loc = cb._find_trajectory(parsed, SESSION_KEY)
        plain = parsed["parts"][loc[0]]["segs"][loc[1]]["plain"]
        scaled = cb._scale_to_viewport(plain, (200, 150))
        obj = json.loads(scaled)
        for pt in obj["mousemove"]:
            self.assertLessEqual(pt["x"], 180)
            self.assertLessEqual(pt["y"], 130)


class LibraryCaptureTests(IsolatedLibraryTests):
    def _unarmed_rich_round(self):
        page = FakePage()
        cb._boosts.clear()
        cb.install_for(page)
        boost = cb._boosts[page]
        boost._session_keys = [SESSION_KEY]
        data = build_synthetic_log3()  # 4-point rich trajectory
        route, request = FakeRoute(), FakeRequest(
            "https://cloudauth-device.cn-shanghai.aliyuncs.com/",
            urllib.parse.urlencode({"Action": "Log3", "Data": data}))
        return page, boost, route, request

    def test_human_round_is_archived_and_forwarded(self):
        page, boost, route, request = self._unarmed_rich_round()
        boost._on_input({}, {"at": int(json.loads(TRAJ)["startTime"]) + 495,
                             "automatic": False})
        page.routes[0][1](route, request)
        # forwarded unchanged
        self.assertIsNone(route.calls[0]["post_data"])
        self.assertEqual(boost.captures, 0)
        self.assertFalse(self.library_path.exists())
        cb.finish_round_for(page, success=True)
        self.assertEqual(boost.captures, 1)
        lib = cb._load_library()
        self.assertIn(TRAJ, lib)
        # a duplicate retry of the same round is not archived twice
        page2, boost2, route2, request2 = self._unarmed_rich_round()
        boost2._on_input({}, {"at": int(json.loads(TRAJ)["startTime"]) + 495,
                              "automatic": False})
        page2.routes[0][1](route2, request2)
        cb.finish_round_for(page2, success=True)
        self.assertEqual(boost2.captures, 0)

    def test_automatic_rich_round_is_not_archived(self):
        page, boost, route, request = self._unarmed_rich_round()
        with cb.automatic_input(page):
            boost._on_input({}, {"at": int(time.time() * 1000), "automatic": True})
        boost._on_route(route, request)
        cb.finish_round_for(page, success=True)
        self.assertEqual(boost.captures, 0)
        self.assertFalse(self.library_path.exists())

    def test_mixed_or_unsuccessful_manual_round_is_not_archived(self):
        for mixed, success in ((True, True), (False, False)):
            page, boost, route, request = self._unarmed_rich_round()
            start = int(json.loads(TRAJ)["startTime"])
            boost._last_automatic_at = start + 600 if mixed else 0
            boost._on_input({}, {"at": start + 495, "automatic": False})
            boost._on_route(route, request)
            cb.finish_round_for(page, success=success)
            self.assertEqual(boost.captures, 0)
            self.assertFalse(self.library_path.exists())

    def test_account_data_roots_keep_libraries_separate(self):
        root = Path(self._tmp.name)
        paths = []
        with patch.object(cb, "LIBRARY_PATH", None):
            for name in ("account-a", "account-b"):
                folder = root / name
                folder.mkdir()
                with patch.dict(os.environ, {"HDUSIGN_ACCOUNT_ROOT": str(folder)}):
                    self.assertEqual(cb._library_file(), folder / "trajectory_library.json")
                    self.assertTrue(cb._append_library_file(TRAJ))
                    paths.append(cb._library_file())
        self.assertNotEqual(paths[0], paths[1])
        self.assertTrue(all(path.is_file() for path in paths))

    def test_injected_round_is_not_recaptured(self):
        page, boost, route, request = self._unarmed_rich_round()
        boost._session_keys = [SESSION_KEY]
        cb.note_click_for(page, 352.0, 211.0)
        page.routes[0][1](route, request)  # armed rewrite
        self.assertEqual(boost.rewrites, 1)
        body = route.calls[0]["post_data"]
        new_data = dict(urllib.parse.parse_qsl(body, keep_blank_values=True))["Data"]
        # retry carrying our injected trajectory, now unarmed: no capture
        route2, request2 = FakeRoute(), FakeRequest(
            "https://cloudauth-device.cn-shanghai.aliyuncs.com/",
            urllib.parse.urlencode({"Action": "Log3", "Data": new_data}))
        page.routes[0][1](route2, request2)
        self.assertIsNone(route2.calls[0]["post_data"])
        self.assertEqual(boost.captures, 0)

    def test_injection_preserves_session_user_agent(self):
        page = FakePage()
        cb._boosts.clear()
        cb.install_for(page)
        boost = cb._boosts[page]
        boost._session_keys = [SESSION_KEY]
        current = json.loads(TRAJ)
        current["userAgent"] = "Mozilla/5.0 SCHOOL-UA"
        current["clientType"] = "desktop"
        injected = boost._build_injection(current, (352.0, 211.0))
        self.assertIsNotNone(injected)
        obj = json.loads(injected)
        self.assertEqual(obj["userAgent"], "Mozilla/5.0 SCHOOL-UA")
        self.assertEqual(obj["clientType"], "desktop")
        # anchored: last event lands just before now
        now = int(time.time() * 1000)
        max_t = max(p["t"] for p in obj["mousemove"])
        self.assertLess(obj["startTime"] + max_t, now)


if __name__ == "__main__":
    unittest.main()
