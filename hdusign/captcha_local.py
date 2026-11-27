# -*- coding: utf-8 -*-
"""本地验证码解题链（在原作者机制之上补强）。

链路：阿里云滑块（本地缺口检测 + 人形拖动）→ 图像点选（本地 OCR 点字）
→ 大模型兜底（Token Plan 兼容接口）→ 交还人工（原版逻辑）。

设计原则：
- 只在原版 _click_verification 点击之后仍然出现验证界面时启用；
- 每次验证最多尝试两次，失败即交还人工，绝不硬刚；
- 所有网络请求仅在启用大模型兜底时发生。
"""
from __future__ import annotations

import base64
import json
import math
import random
import re
import time
from pathlib import Path

_OCR_ENGINE = None

_SCROLL_JS = r"""
() => {
  const vis = (el) => { if (!el) return false; const r = el.getBoundingClientRect(); if (r.width <= 5 || r.height <= 5) return false; let n = el; while (n) { const st = getComputedStyle(n); if (st.display === "none" || st.visibility === "hidden" || Number(st.opacity) === 0) return false; n = n.parentElement; } return true; };
  const trackSel = ["#aliyunCaptcha-sliding-body", ".nc_scale", ".nc-container .nc_scale", "#aliyunCaptcha-sliding-wrapper"];
  for (const s of trackSel) { for (const el of document.querySelectorAll(s)) { if (vis(el)) { el.scrollIntoView({ block: "center" }); return true; } } }
  return false;
}
"""

_PROBE_SLIDER_JS = r"""
() => {
  const vis = (el) => { if (!el) return false; const r = el.getBoundingClientRect(); if (r.width <= 5 || r.height <= 5) return false; let n = el; while (n) { const st = getComputedStyle(n); if (st.display === "none" || st.visibility === "hidden" || Number(st.opacity) === 0) return false; n = n.parentElement; } return true; };
  const trackSel = ["#aliyunCaptcha-sliding-body", ".nc_scale", ".nc-container .nc_scale", "#aliyunCaptcha-sliding-wrapper"];
  let track = null;
  for (const s of trackSel) { for (const el of document.querySelectorAll(s)) { if (vis(el)) { track = el; break; } } if (track) break; }
  if (!track) return null;
  const tr = track.getBoundingClientRect();
  let handle = null;
  for (const el of track.querySelectorAll("#aliyunCaptcha-sliding-slider, .btn_slide, .nc_iconfont.btn_slide, [class*=slider]")) { if (vis(el)) { handle = el; break; } }
  const hr = (handle && vis(handle)) ? handle.getBoundingClientRect() : null;
  let piece = null;
  const explicit = "#aliyunCaptcha-img, #aliyunCaptcha-puzzle, .puzzle-piece, .slider-puzzle img, [class*=piece]";
  const strip = handle ? handle.parentElement : null;
  const small = (el) => {
    if (!vis(el)) return false;
    if (strip && strip.contains(el)) return false;  // 轨道条里的手柄不算拼图块
    const pr = el.getBoundingClientRect();
    return pr.width >= 20 && pr.width <= 90 && pr.height >= 20 && pr.height <= 90 && pr.width < tr.width;
  };
  for (const s of explicit.split(',')) {
    for (const el of document.querySelectorAll(s.trim())) { if (small(el)) { piece = el.getBoundingClientRect(); break; } }
    if (piece) break;
  }
  if (!piece) {
    const scope = track.parentElement || document;
    for (const el of scope.querySelectorAll("img, canvas")) {
      if (!small(el)) continue;
      const pr = el.getBoundingClientRect();
      piece = { x: pr.x, y: pr.y, w: pr.width, h: pr.height };
      break;
    }
  }
  const text = (track.textContent || "").replace(/\s+/g, " ").slice(0, 120);
  return {
    track: { x: tr.x, y: tr.y, w: tr.width, h: tr.height },
    handle: hr ? { x: hr.x, y: hr.y, w: hr.width, h: hr.height } : null,
    piece: piece ? { x: piece.x, y: piece.y, w: piece.width, h: piece.height } : null,
    dragToEnd: /最右/.test(text),
  };
}
"""

_NCC_JS = r"""
({ b64, track, piece }) => new Promise((resolve) => {
  const img = new Image();
  img.onload = () => {
    try {
      // 截图尺寸 = CSS 像素 × devicePixelRatio（Windows 缩放常见 1.25）
      const dpr = img.naturalWidth / (document.documentElement.clientWidth || innerWidth || 1);
      const scale = 0.5;
      const c = document.createElement("canvas");
      c.width = Math.round(track.w * scale);
      c.height = Math.round(track.h * scale);
      const ctx = c.getContext("2d", { willReadFrequently: true });
      ctx.drawImage(img, track.x * dpr, track.y * dpr, track.w * dpr, track.h * dpr, 0, 0, c.width, c.height);
      const bg = ctx.getImageData(0, 0, c.width, c.height).data;
      const pc = document.createElement("canvas");
      pc.width = Math.max(4, Math.round(piece.w * scale));
      pc.height = Math.max(4, Math.round(piece.h * scale));
      const pctx = pc.getContext("2d", { willReadFrequently: true });
      pctx.drawImage(img, piece.x * dpr, piece.y * dpr, piece.w * dpr, piece.h * dpr, 0, 0, pc.width, pc.height);
      const pd = pctx.getImageData(0, 0, pc.width, pc.height).data;
      const gray = (data, i) => (data[i] * 0.299 + data[i + 1] * 0.587 + data[i + 2] * 0.114);
      const bgGray = new Float32Array(c.width * c.height);
      for (let i = 0; i < c.width * c.height; i++) bgGray[i] = gray(bg, i * 4);
      const pGray = new Float32Array(pc.width * pc.height);
      for (let i = 0; i < pc.width * pc.height; i++) pGray[i] = gray(pd, i * 4);
      let pMean = 0; for (let i = 0; i < pGray.length; i++) pMean += pGray[i];
      pMean /= pGray.length;
      let pVar = 0; for (let i = 0; i < pGray.length; i++) pVar += (pGray[i] - pMean) ** 2;
      if (pVar < 1e-4) { resolve({ ok: false }); return; }
      let best = -Infinity, bestX = 0;
      const maxY = c.height - pc.height;
      const selfX0 = piece.x * scale - track.x * scale - pc.width / 2;
      const selfY0 = piece.y * scale - track.y * scale - pc.height / 2;
      for (let y = 0; y <= maxY; y++) {
        for (let x = 0; x <= c.width - pc.width; x++) {
          if (Math.abs(x - selfX0) < pc.width && Math.abs(y - selfY0) < pc.height) continue;
          let sum = 0, bMean = 0, bVar = 0;
          for (let j = 0; j < pc.height; j++) {
            const row = (y + j) * c.width + x;
            for (let i = 0; i < pc.width; i++) {
              const b = bgGray[row + i];
              bMean += b; bVar += b * b;
              sum += b * pGray[j * pc.width + i];
            }
          }
          const n = pGray.length;
          bMean /= n; bVar = bVar / n - bMean * bMean;
          if (bVar < 1e-4) continue;
          const ncc = (sum - n * bMean * pMean) / Math.sqrt(bVar * pVar);
          if (ncc > best) { best = ncc; bestX = x; }
        }
      }
      resolve({ ok: best > 0.3, gapX: bestX / scale, score: best });
    } catch (e) { resolve({ ok: false, error: String(e) }); }
  };
  img.onerror = () => resolve({ ok: false, error: "img load failed" });
  img.src = "data:image/png;base64," + b64;
})
"""


def _human_drag(page, x0: float, y0: float, dx: float, dy: float = 0.0) -> None:
    """人形拖动：先快后慢、带微抖动与小幅回拉。"""
    steps = max(12, int(abs(dx) / 4))
    page.mouse.move(x0, y0)
    page.mouse.down()
    last_x, last_y = x0, y0
    for index in range(1, steps + 1):
        ratio = index / steps
        ease = 1 - (1 - ratio) ** 2.2
        target_x = x0 + dx * ease + random.uniform(-0.8, 0.8)
        target_y = y0 + dy * ease + random.uniform(-1.2, 1.2)
        page.mouse.move(target_x, target_y, steps=1)
        last_x, last_y = target_x, target_y
        if ratio < 0.85:
            page.wait_for_timeout(random.randint(6, 16))
    page.mouse.move(last_x - random.uniform(1.5, 3.0), last_y, steps=2)
    page.wait_for_timeout(random.randint(60, 140))
    page.mouse.up()


def _probe_slider(page):
    """主页与各 iframe 里找滑块，返回 (frame, info)。"""
    candidates = [page] + [frame for frame in list(page.frames) if frame != page.main_frame]
    for owner in candidates:
        try:
            info = owner.evaluate(_PROBE_SLIDER_JS)
        except Exception:  # noqa: BLE001 - 跨域/导航中
            continue
        if info and info.get("handle"):
            return owner, info
    return None, None


def solve_slider(page, *, timeout_s: float = 25.0) -> bool:
    """自动过阿里云滑块：缺口 NCC 模板匹配 + 人形拖动。返回 True=已执行拖动。"""
    frame, info = _probe_slider(page)
    if frame is None or not info:
        return False
    try:
        frame.evaluate(_SCROLL_JS)
    except Exception:  # noqa: BLE001
        pass
    page.wait_for_timeout(400)
    frame, info = _probe_slider(page)
    if frame is None or not info or not info.get("handle"):
        return False
    track, handle, piece = info.get("track") or {}, info.get("handle") or {}, info.get("piece")
    if handle.get("w", 0) < 8 or track.get("w", 0) < 40:
        return False
    x1 = handle["x"] + handle["w"] / 2
    y1 = handle["y"] + handle["h"] / 2
    if info.get("dragToEnd"):
        x2 = track["x"] + track["w"] - 4
        try:
            from hdusign import captcha_boost
            with captcha_boost.automatic_input(page):
                captcha_boost.note_click_for(page, x1, y1)
                _human_drag(page, x1, y1, x2 - x1)
        except Exception:  # noqa: BLE001
            _human_drag(page, x1, y1, x2 - x1)
        return True
    if not piece:
        # 没有独立拼图块：整图交给大模型找缺口，找不到就交还人工
        try:
            shot = page.screenshot(timeout=5000)
            ratio = _vision_gap_ratio(shot)
        except Exception:  # noqa: BLE001
            ratio = None
        if not ratio:
            return False
        travel = max(0.0, track["w"] - handle["w"])
        x2 = x1 + travel * ratio
        try:
            from hdusign import captcha_boost
            with captcha_boost.automatic_input(page):
                captcha_boost.note_click_for(page, x1, y1)
                _human_drag(page, x1, y1, x2 - x1)
        except Exception:  # noqa: BLE001
            _human_drag(page, x1, y1, x2 - x1)
        return True
    # 缺口匹配（ego 版验证过的 NCC 方案）
    gap = {"ok": False}
    try:
        shot = page.screenshot(timeout=5000)
        gap = frame.evaluate(_NCC_JS, {"b64": base64.b64encode(shot).decode(),
                                       "track": track, "piece": piece})
    except Exception:  # noqa: BLE001
        gap = {"ok": False}
    if not gap.get("ok"):
        # 大模型兜底：直接看整页截图找缺口
        try:
            ratio = _vision_gap_ratio(shot)
        except Exception:  # noqa: BLE001
            ratio = None
        if ratio:
            travel = max(0.0, track["w"] - handle["w"])
            x2 = x1 + travel * ratio
            try:
                from hdusign import captcha_boost
                with captcha_boost.automatic_input(page):
                    captcha_boost.note_click_for(page, x1, y1)
                    _human_drag(page, x1, y1, x2 - x1)
            except Exception:  # noqa: BLE001
                _human_drag(page, x1, y1, x2 - x1)
            return True
        return False
    gap_x = float(gap.get("gapX") or 0)
    # 把「拼图块」对齐到缺口：位移 = 缺口位置 - 拼图块初始相对位置
    piece_left = (piece or {}).get("x", track["x"]) - track["x"] if piece else 0.0
    dx = max(0.0, min(track["w"] - handle["w"] - 2.0, gap_x - piece_left))
    if dx < 8:
        return False
    try:
        from hdusign import captcha_boost
        with captcha_boost.automatic_input(page):
            captcha_boost.note_click_for(page, x1, y1)
            _human_drag(page, x1, y1, dx)
    except Exception:  # noqa: BLE001
        _human_drag(page, x1, y1, dx)
    return True


def _load_solver_config() -> dict:
    """读取 auto_sign.captcha_solver 配置；缺省关闭。"""
    try:
        import yaml

        from hdusign.runtime import application_root
        cfg_path = Path(application_root()) / "config.yaml"
        if not cfg_path.is_file():
            return {}
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        return (cfg.get("auto_sign") or {}).get("captcha_solver") or {}
    except Exception:  # noqa: BLE001
        return {}


def _vision_gap_ratio(png: bytes):
    """大模型兜底：让多模态模型给出缺口水平位置比例。"""
    solver = _load_solver_config()
    if not solver.get("enabled") or not solver.get("api_key"):
        return None
    try:
        import requests
    except Exception:  # noqa: BLE001
        return None
    prompt = ("这是滑块验证码的背景图。请找出图中缺口(凹槽/暗色缺口)的水平中心位置，"
              "输出 0 到 1 之间的比例数字（缺口中心距图片左边缘 ÷ 图片宽度），"
              "只输出这个数字，不要其他文字。")
    try:
        response = requests.post(
            str(solver.get("base_url") or "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1")
            .rstrip("/") + "/chat/completions",
            headers={"Authorization": "Bearer " + str(solver["api_key"])},
            json={
                "model": str(solver.get("model") or "qwen3.8-flash"),
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {
                        "url": "data:image/png;base64," + base64.b64encode(png).decode()}},
                ]}],
                "timeout": float(solver.get("timeout_seconds") or 20),
            },
            timeout=float(solver.get("timeout_seconds") or 20) + 10,
        )
        body = response.json()
        text = str((body.get("choices") or [{}])[0].get("message", {}).get("content") or "")
        match = re.search(r"(0\.\d+|1\.0+|0|1)", text)
        return float(match.group(1)) if match else None
    except Exception:  # noqa: BLE001
        return None


def _ocr_points(png: bytes) -> list[dict]:
    """本地 OCR：返回 [{text, x, y}]；失败返回空列表。"""
    global _OCR_ENGINE
    try:
        from io import BytesIO

        from PIL import Image
        from rapidocr_onnxruntime import RapidOCR
    except Exception:  # noqa: BLE001
        return []
    engine = _OCR_ENGINE
    if engine is None:
        try:
            engine = RapidOCR()
            _OCR_ENGINE = engine
        except Exception:  # noqa: BLE001
            return []
    try:
        import numpy as np

        image = Image.open(BytesIO(png)).convert("RGB")
        # 该版本 RapidOCR 只接受 ndarray/bytes/path，不接受 PIL.Image；
        # 点选图的字是孤立单字，默认阈值检不出来，需要放宽检测阈值
        result, _ = engine(np.array(image), box_thresh=0.3,
                           unclip_ratio=1.6, text_score=0.3)
    except Exception:  # noqa: BLE001
        return []
    points = []
    for item in result or []:
        try:
            box, text, _score = item[0], item[1], item[2]
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            points.append({"text": str(text).strip(),
                           "x": sum(xs) / len(xs), "y": sum(ys) / len(ys)})
        except Exception:  # noqa: BLE001
            continue
    return points


def solve_point_click(page) -> bool:
    """图像点选验证码：提示文字从弹窗读取，只对「图片区域」做 OCR 并按顺序点击。"""
    try:
        container = page.locator(
            "text=/请依次点击|请依次选择|点击图中|依次点击/"
        ).filter(visible=True).first
        if not container.count():
            return False
        wrapper = container.locator("xpath=ancestor::*[self::div or self::section][1]")
        box = wrapper.bounding_box(timeout=2000)
        if not box or box["width"] < 100:
            return False
        prompt_text = container.inner_text()
    except Exception:  # noqa: BLE001
        return False
    match = re.search(r"依次点击[：:]?\s*(.+)", prompt_text)
    if not match:
        match = re.search(r"点击图中\s*(.+)", prompt_text)
    if not match:
        return False
    chars = [ch for ch in match.group(1) if ch.strip() and ch not in "，。；、 "]
    if not chars:
        return False
    # 只取图片区域做 OCR，避免点到提示文字里的同样的字
    image_locator = None
    image_box = None
    try:
        for candidate in wrapper.locator("img, canvas").all():
            try:
                if not candidate.is_visible():
                    continue
                candidate_box = candidate.bounding_box(timeout=1500)
                if candidate_box and candidate_box["width"] >= 80 and candidate_box["height"] >= 60:
                    image_locator, image_box = candidate, candidate_box
                    break
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        image_locator = None
    if image_locator is None or image_box is None:
        return False
    try:
        shot = image_locator.screenshot(timeout=4000)
    except Exception:  # noqa: BLE001
        return False
    points = _ocr_points(shot)
    if not points:
        return False
    clicked = 0
    for ch in chars:
        hit = next((p for p in points if p["text"].strip() == ch), None)
        if hit is None:
            hit = next((p for p in points if ch in p["text"]), None)
        if hit is None:
            continue
        try:
            from hdusign import captcha_boost
            with captcha_boost.automatic_input(page):
                page.mouse.click(image_box["x"] + hit["x"], image_box["y"] + hit["y"])
            clicked += 1
            page.wait_for_timeout(random.randint(250, 600))
        except Exception:  # noqa: BLE001
            continue
    if clicked == 0:
        return False
    if clicked < len(chars):
        # 有字没识别到：不点确认，交还人工，避免提交错误答案
        return False
    for label in ("确认", "确定", "提交"):
        try:
            button = wrapper.get_by_text(label, exact=True).filter(visible=True).first
            if button.count():
                button.click(timeout=1500)
                break
        except Exception:  # noqa: BLE001
            continue
    return True


def try_solve(page, *, attempts: int = 2, timeout_s: float = 25.0) -> bool:
    """按链路尝试自动过验证。返回 True 表示已尝试过（结果需由调用方核对）。"""
    if not page_is_visible(page):
        return False
    for _ in range(max(1, attempts)):
        if solve_slider(page, timeout_s=timeout_s):
            page.wait_for_timeout(1200)
            if not page_is_visible(page):
                return True
        if solve_point_click(page):
            page.wait_for_timeout(1200)
            if not page_is_visible(page):
                return True
    return False


def page_is_visible(page) -> bool:
    """验证界面是否仍然可见（复用原版检测逻辑的轻量版）。"""
    selector = ("#aliyunCaptcha-sliding-wrapper, #aliyunCaptcha-window-popup, "
                ".nc-container, #baxia-dialog-content, [role=dialog]:has-text('安全验证')")
    try:
        return bool(page.locator(selector).evaluate_all("""els => els.some(el => {
            const r = el.getBoundingClientRect();
            if (r.width <= 1 || r.height <= 1) return false;
            const s = getComputedStyle(el);
            return s.display !== 'none' && s.visibility !== 'hidden' && Number(s.opacity) !== 0;
        })"""))
    except Exception:  # noqa: BLE001
        return False
