"""Per-account event journals and bounded, opt-in decoded-audio diagnostics."""
from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path
import re
import threading
import time
import wave

import numpy as np

RATE = 16000
ZONE = dt.timezone(dt.timedelta(hours=8))


def stamp(value):
    return dt.datetime.fromtimestamp(value, ZONE).isoformat(timespec="milliseconds")


def new_run_directory(root):
    import tempfile
    folder = Path(root) / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    prefix = dt.datetime.now(ZONE).strftime("%Y%m%d_%H%M%S_")
    return Path(tempfile.mkdtemp(prefix=prefix, dir=folder))


class Journal:
    def __init__(self, directory, wall=None, mono=None):
        self.directory = Path(directory)
        self.wall = wall or time.time
        self.mono = mono or time.monotonic
        self.lock = threading.RLock()
        self.file = (self.directory / "events.jsonl").open("a", encoding="utf-8", buffering=1)
        self.failed = False
        self.closed = False
        self.first = {}

    def record(self, event, *, at=None, **fields):
        at = self.wall() if at is None else at
        item = dict(event=event, timestamp=stamp(at), unix_time=at,
                    monotonic_seconds=self.mono(), **fields)
        with self.lock:
            self.first.setdefault(event, item)
            if self.closed or self.failed:
                return item
            try:
                self.file.write(json.dumps(item, ensure_ascii=False, allow_nan=False) + "\n")
                self.file.flush()
            except (OSError, ValueError):
                self.failed = True
        return item

    def close(self):
        with self.lock:
            if not self.closed:
                self.closed = True
                try:
                    self.file.close()
                except OSError:
                    pass


class JournalOutput:
    """Only installed inside one worker; stdout timing never crosses accounts."""
    def __init__(self, original, journal, stream):
        self.original, self.journal, self.stream = original, journal, stream
        self.pending = ""
        self.pending_at = None
        self.lock = threading.RLock()

    def __getattr__(self, key):
        return getattr(self.original, key)

    def write(self, text):
        with self.lock:
            try:
                count = self.original.write(text) if self.original is not None else len(text)
            except (OSError, ValueError):
                count = len(text)
            # A CR terminates the preceding partial, just like the GUI reader.
            for piece in re.split("([\r\n])", text):
                if piece in ("\r", "\n"):
                    self._commit()
                elif piece:
                    if self.pending_at is None:
                        self.pending_at = self.journal.wall()
                    self.pending += piece
            return count

    def _commit(self):
        if self.pending.strip():
            self.journal.record("console", at=self.pending_at, stream=self.stream,
                                text=self.pending.rstrip())
        self.pending, self.pending_at = "", None

    def flush(self):
        with self.lock:
            if self.original is not None:
                try:
                    self.original.flush()
                except (OSError, ValueError):
                    pass

    def finish(self):
        with self.lock:
            self._commit()
            self.flush()


class Diagnostics:
    def __init__(self, config, plan=None, *, journal=None, wall=None, mono=None):
        self.wall, self.mono = wall or time.time, mono or time.monotonic
        self.enabled = bool((config.get("diagnostics") or {}).get("enabled", False))
        self.journal = journal or config.get("_run_journal")
        self.owns_journal = False
        if self.journal is None and self.enabled:
            from hdusign.runtime import data_root
            try:
                self.journal = Journal(new_run_directory(data_root()), self.wall, self.mono)
                self.owns_journal = True
            except OSError:
                self.enabled = False
                print("[warn] 无法创建诊断目录，本次继续监听，诊断录音未开启。")
        self.directory = self.journal.directory if self.journal else None
        self.started_at = self.wall()
        self.started_mono = self.mono()
        self.last_metric = self.started_mono
        self.first = {key: value for key, value in (self.journal.first.items() if self.journal else [])
                      if key in ("task_requested", "task_started")}
        self.current_id = ""
        self.source_first = True
        self.audio_seconds = self.processed_seconds = self.processing_seconds = 0.0
        self.max_call = self.skipped_seconds = self.last_skipped = 0.0
        self.energy = self.count = self.peak = self.quiet_samples = 0
        self.previous_partial = ""
        self.wav = None
        self.wav_failed = False
        self.frames = 0
        self.recording_first = self.recording_last = None
        self.last_recording_end = None
        self.last_recorded_id = None
        self.recording_segments = []
        self.recording_done = False
        self.closed = False
        context = config.get("notification_context") or {}
        selected = config.get("continuous_course") or {}
        self.account = str(context.get("username") or "")
        self.course = str(context.get("course_name") or selected.get("title") or "")
        if plan:
            start = plan.upcoming_start(self.started_at)
            if self.started_at < start + 60:
                self.window_start, self.window_end = start - 120, start + 60
                self.window_kind = "scheduled_class_start"
            else:
                self.window_start, self.window_end = self.started_at, self.started_at + 180
                self.window_kind = "midclass_monitor_start"
        else:
            self.window_start, self.window_end = self.started_at, self.started_at + 180
            self.window_kind = "monitor_start_without_timetable"
        self.mark("monitor_entered", account=self.account, course=self.course,
                  recording_enabled=self.enabled, recording_window_kind=self.window_kind,
                  recording_window_start=stamp(self.window_start), recording_window_end=stamp(self.window_end))
        if self.enabled and self.directory:
            print(f"[i] 诊断模式已开启：保存启动音频和识别指标，目录 {self.directory}")

    def mark(self, event, **fields):
        item = self.journal.record(event, **fields) if self.journal else {
            "event": event, "timestamp": stamp(self.wall()), "unix_time": self.wall(), **fields}
        if event in ("monitor_entered", "model_loading", "model_ready", "first_audio",
                     "first_nonempty_text", "first_partial", "first_final"):
            self.first.setdefault(event, item)
        return item

    def begin_stream(self, course_id):
        self.current_id = str(course_id)
        self.source_first, self.last_skipped = True, 0.0
        self.mark("stream_opening", course_id=self.current_id)

    def audio(self, samples, source=None):
        if self.source_first:
            self.source_first = False
            self.mark("first_audio", course_id=self.current_id)
        self.audio_seconds += len(samples) / RATE
        skipped = float(getattr(source, "skipped_seconds", 0.0)) if source is not None else 0.0
        self.skipped_seconds += max(0.0, skipped - self.last_skipped)
        self.last_skipped = skipped
        if not self.enabled:
            return
        data = np.nan_to_num(np.asarray(samples, dtype=np.float32), nan=0.0, posinf=1.0, neginf=-1.0)
        squared = float(np.dot(data, data))
        self.energy += squared
        self.count += len(data)
        self.peak = max(self.peak, float(np.max(np.abs(data))) if len(data) else 0.0)
        if len(data) and math.sqrt(squared / len(data)) < 10**(-45 / 20):
            self.quiet_samples += len(data)
        self._record_audio(data)

    def _record_audio(self, data):
        if self.wav_failed or self.recording_done or self.directory is None or self.frames >= 180 * RATE:
            return
        now = self.wall()
        begin, end = now - len(data) / RATE, now
        left, right = max(begin, self.window_start), min(end, self.window_end)
        if right <= left:
            if self.wav is not None and now >= self.window_end:
                self._close_wav()
                self.recording_done = True
            return
        first = max(0, min(len(data), round((left - begin) * RATE)))
        last = max(first, min(len(data), round((right - begin) * RATE)))
        part = data[first:last][:180 * RATE - self.frames]
        if not len(part):
            return
        try:
            if self.wav is None:
                self.wav = wave.open(str(self.directory / "startup-audio.wav"), "wb")
                self.wav.setnchannels(1)
                self.wav.setsampwidth(2)
                self.wav.setframerate(RATE)
            if (self.last_recording_end is None or abs(left - self.last_recording_end) > 1.0
                    or self.last_recorded_id != self.current_id):
                segment = dict(start_frame=self.frames, sample_received_at=stamp(left), course_id=self.current_id)
                self.recording_segments.append(segment)
                self.mark("recording_segment", **segment)
            pcm = np.rint(np.clip(part, -1, 32767 / 32768) * 32768).astype("<i2")
            self.wav.writeframes(pcm.tobytes())
            self.frames += len(part)
            self.recording_first = left if self.recording_first is None else self.recording_first
            self.recording_last = left + len(part) / RATE
            self.last_recording_end = self.recording_last
            self.last_recorded_id = self.current_id
            if self.frames >= 180 * RATE or end >= self.window_end:
                self._close_wav()
                self.recording_done = True
        except (OSError, wave.Error):
            self.wav_failed = True
            self._close_wav()
            self.mark("recording_failed", reason="audio_file_write_failed")
            print("[warn] 诊断音频保存失败，继续监听和识别。")

    def _close_wav(self):
        if self.wav is not None:
            try:
                self.wav.close()
            except (OSError, wave.Error):
                self.wav_failed = True
            self.wav = None

    def asr_result(self, samples, seconds, partial, finals):
        self.processed_seconds += len(samples) / RATE
        self.processing_seconds += max(0.0, seconds)
        self.max_call = max(self.max_call, seconds)
        nonempty = partial or next((text for text in finals if text.strip()), "")
        if nonempty and "first_nonempty_text" not in self.first:
            self.mark("first_nonempty_text", course_id=self.current_id, text=nonempty)
        if partial and "first_partial" not in self.first:
            self.mark("first_partial", course_id=self.current_id, text=partial)
        if self.enabled and partial and partial != self.previous_partial:
            self.mark("partial_transcript", course_id=self.current_id, text=partial,
                      audio_seconds_delivered=round(self.audio_seconds, 3))
        self.previous_partial = partial
        if self.enabled and self.mono() - self.last_metric >= 5:
            self.metric()

    def final(self, text):
        if "first_nonempty_text" not in self.first:
            self.mark("first_nonempty_text", course_id=self.current_id, text=text)
        if "first_final" not in self.first:
            self.mark("first_final", course_id=self.current_id, text=text)
        self.mark("final_transcript", course_id=self.current_id, text=text,
                  audio_seconds_delivered=round(self.audio_seconds, 3))

    def metric(self):
        rms = math.sqrt(self.energy / self.count) if self.count else 0.0
        fields = dict(course_id=self.current_id,
                      audio_seconds_delivered=round(self.audio_seconds, 3),
                      asr_audio_seconds=round(self.processed_seconds, 3),
                      asr_processing_seconds=round(self.processing_seconds, 3),
                      asr_processing_rtf=round(self.processing_seconds / self.processed_seconds, 4) if self.processed_seconds else 0,
                      max_asr_call_seconds=round(self.max_call, 3),
                      skipped_audio_seconds=round(self.skipped_seconds, 3),
                      rms_dbfs=round(20 * math.log10(max(rms, 1e-6)), 1),
                      peak_dbfs=round(20 * math.log10(max(self.peak, 1e-6)), 1),
                      low_level_sample_ratio=round(self.quiet_samples / self.count, 4) if self.count else 0,
                      metric_audio_seconds=round(self.count / RATE, 3))
        self.mark("audio_metrics", **fields)
        self.energy = self.count = self.peak = self.quiet_samples = 0
        self.last_metric = self.mono()
        return fields

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.recording_done = True
        self._close_wav()
        metrics = self.metric() if self.enabled else {}
        self.mark("monitor_finished", recorded_audio_seconds=round(self.frames / RATE, 3), **metrics)
        summary = dict(account=self.account, course=self.course, recording_enabled=self.enabled,
                       timings=self.first, metrics=metrics, recorded_audio_seconds=round(self.frames / RATE, 3),
                       recording_failed=self.wav_failed, recording_window_kind=self.window_kind,
                       window_start=stamp(self.window_start), window_end=stamp(self.window_end),
                       recording_first=stamp(self.recording_first) if self.recording_first is not None else None,
                       recording_last=stamp(self.recording_last) if self.recording_last is not None else None,
                       recording_segments=self.recording_segments)
        if self.directory:
            try:
                (self.directory / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), "utf-8")
                labels = {"task_requested": "主窗口发起任务", "task_started": "账号子进程进入任务",
                          "monitor_entered": "进入监听任务", "model_loading": "开始加载识别模型",
                          "model_ready": "识别模型准备完成", "first_audio": "首次收到解码音频",
                          "first_nonempty_text": "首次识别出文字", "first_partial": "首次实时草稿",
                          "first_final": "首次完整转写"}
                lines = [f"账号：{self.account}", f"课程：{self.course}", ""]
                lines += [f"{label}：{self.first[event]['timestamp'] if event in self.first else '本次未发生'}"
                          for event, label in labels.items()]
                lines += ["", f"诊断录音：{'开启' if self.enabled else '关闭'}；实际保存 {self.frames / RATE:.2f} 秒",
                          f"计划采样窗口：{stamp(self.window_start)} 至 {stamp(self.window_end)}",
                          f"累计送入识别的音频：{self.processed_seconds:.2f} 秒",
                          f"识别调用累计耗时：{self.processing_seconds:.2f} 秒",
                          f"累计跳过积压音频：{self.skipped_seconds:.2f} 秒",
                          "", "低音量不等于没有人声；处理耗时比值大于 1 表示识别处理速度不足。",
                          "录音为本机实际交给识别器的音频，不能据此确定学校原始采集时间。",
                          "断流缺口在 events.jsonl / summary.json 标记，不补造静音；WAV 会拼接已收到的片段。"]
                (self.directory / "诊断摘要.txt").write_text("\n".join(lines) + "\n", "utf-8")
            except OSError:
                print("[warn] 诊断摘要保存失败，已写入的日志仍可查看。")
        if self.owns_journal:
            self.journal.close()
