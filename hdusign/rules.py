"""Keyword and semantic matching, plus spoken attendance-code extraction."""
from __future__ import annotations

import json
import re
import time
from collections import deque
from typing import List, Optional, Sequence, Tuple

_RE_NON_ALNUM_CJK = re.compile(r"[^0-9a-zA-Z一-鿿]+")


def normalize(text) -> str:
    return _RE_NON_ALNUM_CJK.sub("", str(text).lower())


class RuleMatcher:
    """strong: any term hit -> high; weak_groups: all terms of a group -> medium.
    strong terms prefixed with "re:" are treated as regex (e.g. "re:(?<![0-9])点到")."""

    def __init__(self, strong: Sequence[str], weak_groups: Sequence[Sequence[str]]):
        self.strong = [normalize(t) for t in strong if t.strip() and not t.startswith("re:")]
        self.strong_re = [re.compile(t[3:]) for t in strong if t.startswith("re:")]
        self.weak_groups = [[normalize(t) for t in g] for g in weak_groups if g]

    def match(self, text: str) -> Optional[Tuple[str, str]]:
        t = normalize(text)
        if not t:
            return None
        for term in self.strong:
            if term and term in t:
                return ("high", f"强关键词:{term}")
        for pat in self.strong_re:
            if pat.search(t):
                return ("high", f"强规则:{pat.pattern}")
        for group in self.weak_groups:
            hit = [g for g in group if g in t]
            if len(hit) == len(group):
                return ("medium", f"弱词组合:{group}")
        return None


class SemanticMatcher:
    """Optional cosine-similarity matcher against template sentences (fastembed).

    Very short fragments ("我们", "到首先") score high against any template, so
    sentences shorter than `min_length` characters are never matched."""

    def __init__(self, templates: Sequence[str], model: str, threshold: float,
                 min_length: int = 6):
        self.threshold = threshold
        self.min_length = int(min_length)
        self.templates = list(templates)
        from fastembed import TextEmbedding  # lazy; only when enabled

        from hdusign.runtime import semantic_model_options

        self._model = TextEmbedding(model_name=model, **semantic_model_options(model))

        import numpy as np

        embs = list(self._model.embed(self.templates))
        self._t_embs = np.array(embs)
        self._t_embs /= np.linalg.norm(self._t_embs, axis=1, keepdims=True) + 1e-9

    def match(self, text: str) -> Optional[Tuple[str, str]]:
        import numpy as np

        if len(normalize(text)) < self.min_length:
            return None
        emb = next(iter(self._model.embed([text])))
        emb = emb / (np.linalg.norm(emb) + 1e-9)
        sims = self._t_embs @ emb
        i = int(np.argmax(sims))
        if sims[i] >= self.threshold:
            return ("semantic", f"语义[{self.templates[i]}]得分{sims[i]:.2f}")
        return None


def build_matchers(cfg: dict):
    rules = cfg.get("rules", {})
    matchers: List = [RuleMatcher(rules.get("strong", []), rules.get("weak_groups", []))]
    sem = rules.get("semantic", {})
    if sem.get("enabled"):
        try:
            matchers.append(SemanticMatcher(sem.get("templates", []), sem.get("model"),
                                            float(sem.get("threshold", 0.78)),
                                            int(sem.get("min_length", 6))))
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 语义匹配初始化失败, 回退到纯规则: {e}")
    return matchers


CN_DIGITS = {"零": "0", "〇": "0", "一": "1", "幺": "1", "二": "2", "两": "2",
             "三": "3", "四": "4", "五": "5", "六": "6", "七": "7", "八": "8",
             "九": "9"}
_DIGIT_CHARS = "0-9" + "".join(CN_DIGITS)
_DIGIT_RUN = re.compile(f"[{_DIGIT_CHARS}]+")
_CODE = re.compile(f"(?<![{_DIGIT_CHARS}])[0-9]{{4}}(?![{_DIGIT_CHARS}])")
_NUMERIC_PREFIX = re.compile(f"[{_DIGIT_CHARS}][\\s.,，。．·、]*$")


def _repeated_code(run: str) -> Optional[str]:
    """Lecturers repeat a code ("六六零九六六零九") and the recognizer often
    merges the repetitions into one run, sometimes cutting the last one short
    ("九九三八九九三"). Return the code when the run consists of nothing but
    that code repeated (the final repetition may be truncated to 2+ digits)."""
    if len(run) <= 4 or len(run) > 12:
        return None
    code = run[:4]
    rest = run[4:]
    while rest.startswith(code):
        rest = rest[4:]
    if rest == "" or (2 <= len(rest) < 4 and code.startswith(rest)):
        return code
    return None


def _convert_run(s: str) -> str:
    """Pure-ASCII runs stay as-is; four digits may mix Chinese and ASCII.
    Longer Chinese runs only count when they are one four-digit code repeated;
    anything else (phone numbers, years, garbles) is left untouched."""
    if s.isdigit():
        return s
    if len(s) == 4:
        return "".join(CN_DIGITS.get(ch, ch) for ch in s)
    if all(ch in CN_DIGITS for ch in s) and (code := _repeated_code(s)):
        return "".join(CN_DIGITS[ch] for ch in code)
    return s


def extract_codes(text: str) -> Tuple[str, List[str]]:
    """Return (digit-normalized text, list of 4-digit codes)."""
    converted = _DIGIT_RUN.sub(lambda m: _convert_run(m.group()), text)
    return converted, _CODE.findall(converted)


class SignInWatcher:
    """Collects recent sentences; on trigger keeps a transcript open for
    `window_seconds` and extracts 4-digit codes from everything heard.

    `clock` returns the current time in seconds. Live monitoring uses the wall
    clock; file replay passes the audio position so that windows and
    de-duplication follow the recording rather than decoding speed."""

    def __init__(self, window_seconds: float = 60, context_seconds: float = 30,
                 code_dedup: float = 60, log_file: str = "codes.jsonl",
                 standalone_code: bool = True, clock=None, *, early_code: bool = False,
                 early_stable_seconds: float = 0.4):
        self.window_seconds = window_seconds
        self.context_seconds = context_seconds
        self.code_dedup = code_dedup
        self.log_file = log_file
        self.standalone_code = standalone_code
        self.clock = clock or time.time
        self.recent: deque = deque()          # (ts, text) finals for context dump
        self.watch_until = 0.0
        self._last_code: dict = {}
        self.codes_found: List[Tuple[str, str]] = []  # (code, source text)
        self.on_code = lambda code, text: None  # set by caller
        self.early_code = early_code
        self.early_stable_seconds = max(0.0, float(early_stable_seconds))
        self._partial_revision = -1
        self._candidates: dict = {}
        self._early_sent: set[str] = set()

    @property
    def active(self) -> bool:
        return self.clock() < self.watch_until

    def discard_partial(self) -> None:
        """Discard unfinished recognition after an audio gap; keep confirmed codes."""
        self._partial_revision = -1
        self._candidates.clear()
        self._early_sent.clear()

    def trigger(self, text: str, reason: str) -> None:
        now = self.clock()
        self.watch_until = now + self.window_seconds
        print(f"\n{'=' * 62}")
        print(f"[监码窗口开启 {time.strftime('%H:%M:%S')}] 触发: {reason}")
        print(f"--- 触发前 {int(self.context_seconds)}s 上下文 ---")
        for ts, t in self.recent:
            print(f"  -{now - ts:4.0f}s | {t}")
        print(f"--- 触发句 ---")
        print(f"   0s  | {text}")
        print(f"接下来 {int(self.window_seconds)}s 内的所有识别文本将实时输出, 关注4位数字")
        print(f"{'=' * 62}\n")

    def feed_partial(self, text: str, *, revision: int | None, audio_time: float,
                     cue: bool = False) -> List[Tuple[str, str]]:
        """Confirm a bounded digit span without waiting for the entire sentence.

        A bare four-digit tail waits for final transcription. Early confirmation needs
        following nonnumeric speech or a second complete, identical reading,
        plus stability across actual decoder updates. Repeated calls while the
        decoder is buffering do not count as additional evidence.
        """
        if type(revision) is not int:
            return []
        if revision < self._partial_revision:
            self._candidates.clear()
            self._early_sent.clear()
        if revision == self._partial_revision:
            return []
        self._partial_revision = revision
        norm, codes = extract_codes(text)
        residue = norm.replace(codes[0], "", 1).strip() if codes else norm
        eligible = self.active or cue or (self.standalone_code and len(residue) <= 4)
        if not eligible:
            self._candidates.clear()
            return []

        candidates = {}
        ready = []
        for run in _DIGIT_RUN.finditer(text):
            raw = run.group()
            code = _convert_run(raw)
            if code not in codes or _NUMERIC_PREFIX.search(text[:run.start()]):
                continue
            key = (run.start(), code)
            since, updates = self._candidates.get(key, (audio_time, 0))
            candidates[key] = (since, updates + 1)
            suffix = normalize(text[run.end():])
            # Decimal/unit continuations and another digit run are not a safe
            # boundary (e.g. 1234.56, 1234万元, or 1381 2345678).
            closed = len(raw) == 4 and len(suffix) >= 2 and suffix[0] not in (
                "0123456789" + "".join(CN_DIGITS) + "十百千万亿兆点年月日时分秒元号个位")
            repeated = len(raw) >= 8 and len(raw) % 4 == 0 and _repeated_code(raw) is not None
            if (self.early_code and (closed or repeated) and updates >= 1
                    and audio_time - since >= self.early_stable_seconds
                    and code not in self._early_sent):
                ready.append(code)
        self._candidates = candidates
        found = self._commit(text, ready)
        self._early_sent.update(code for code, _ in found)
        return found

    def feed(self, text: str, final: bool = True) -> List[Tuple[str, str]]:
        """Commit a final utterance; raw partials use feed_partial's confirmation.

        A partial four-digit run can still grow into a phone number or change
        its digits. Only feed_partial's confirmed spans may be submitted early.
        """
        if not final:
            return []
        early_sent, self._early_sent = self._early_sent, set()
        self._candidates.clear()
        now = self.clock()
        self.recent.append((now, text))
        while self.recent and now - self.recent[0][0] > self.context_seconds:
            self.recent.popleft()
        if not self.active and not self.standalone_code:
            return []

        norm, codes = extract_codes(text)
        if not self.active:
            # 窗口未开时只认" essentially 就是一个码"的短句, 避免长句里的年份等误报
            residue = norm.replace(str(codes[0]), "", 1) if codes else norm
            if not codes or len(residue.strip()) > 4:
                return []
        return self._commit(text, [code for code in codes if code not in early_sent])

    def _commit(self, text: str, codes: List[str]) -> List[Tuple[str, str]]:
        now = self.clock()
        found = []
        for c in codes:
            last = self._last_code.get(c)
            if last is not None and now - last < self.code_dedup:
                continue
            self._last_code[c] = now
            found.append((c, text))
            self.codes_found.append((c, text))
            try:
                with open(self.log_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"ts": now, "time": time.strftime("%H:%M:%S"),
                                        "code": c, "text": text},
                                       ensure_ascii=False) + "\n")
            except OSError:
                pass
        return found
