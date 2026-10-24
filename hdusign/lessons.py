"""Timetabled two/three-lesson blocks and cancellable media deadlines."""
from __future__ import annotations

from dataclasses import dataclass, replace
import datetime as dt
import math
import re
import time

ZONE = dt.timezone(dt.timedelta(hours=8))
MAX_BREAK = 20 * 60
EARLY_START = 120.0
LATE_END = 120.0


def group_lessons(courses):
    """Merge only complete, consecutive, same-class 45-minute lessons.

    HDU's list has one ID per timetable cell. The user's verified playback
    convention is one stream for the first two cells, then first ID + 2.
    Require that third ID to exist in the list instead of guessing a course.
    """
    buckets, singles = {}, []
    for course in courses:
        if not all((course.tecl_id, course.subject_id, course.room_id, course.term_id,
                    course.number)) or course.end - course.start != 45 * 60:
            singles.append(course)
            continue
        key = (course.origin, course.tecl_id, course.subject_id, course.room_id,
               course.term_id, course.title,
               dt.datetime.fromtimestamp(course.start, ZONE).date())
        buckets.setdefault(key, []).append(course)

    def finish(run):
        if len(run) not in (2, 3) or any(
                int(c.course_id) != int(run[0].course_id) + i for i, c in enumerate(run)):
            singles.extend(run)
            return
        first, last = run[0], run[-1]
        singles.append(replace(
            first, end=last.end,
            section=f"第{first.number}–{last.number}节 · {len(run)}节连堂",
            lesson_ids=tuple(c.course_id for c in run),
            lesson_starts=tuple(c.start for c in run),
            lesson_ends=tuple(c.end for c in run)))

    for bucket in buckets.values():
        run = []
        for course in sorted(bucket, key=lambda c: (c.start, c.course_id)):
            if run and not (course.number == run[-1].number + 1
                            and 0 <= course.start - run[-1].end <= MAX_BREAK):
                finish(run)
                run = []
            run.append(course)
        if run:
            finish(run)
    return sorted(singles, key=lambda c: (c.start, c.end, c.course_id))


@dataclass(frozen=True)
class LessonPlan:
    ids: tuple[str, ...]
    starts: tuple[float, ...]
    ends: tuple[float, ...]

    @classmethod
    def from_mapping(cls, value, course_id):
        if not value:
            return None
        if not isinstance(value, dict):
            raise ValueError("连堂课资料无效，请重新刷新并选择课程。")
        ids = value.get("lesson_ids") or ()
        starts = value.get("lesson_starts") or ()
        ends = value.get("lesson_ends") or ()
        if not ids and not starts and not ends:
            if str(value.get("course_id")) != str(course_id):
                return None
            start, end = value.get("start"), value.get("end")
            if any(isinstance(v, bool) or not isinstance(v, (int, float))
                   or not math.isfinite(v) for v in (start, end)) or end <= start:
                raise ValueError("课程时间无效，请重新刷新课程。")
            return cls((str(course_id),), (start,), (end,))
        if (not all(isinstance(v, (tuple, list)) for v in (ids, starts, ends))
                or len(ids) not in (2, 3) or len(starts) != len(ids) or len(ends) != len(ids)):
            raise ValueError("连堂课资料不完整，请重新刷新并选择课程。")
        if any(not isinstance(v, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", v) for v in ids):
            raise ValueError("连堂课编号无效，请重新刷新课程。")
        if str(course_id) not in ids or any(int(v) != int(ids[0]) + i for i, v in enumerate(ids)):
            raise ValueError("连堂课编号与链接不一致，请重新选择课程。")
        if any(isinstance(v, bool) or not isinstance(v, (int, float))
               or not math.isfinite(v) for v in (*starts, *ends)):
            raise ValueError("连堂课时间无效，请重新刷新课程。")
        if any(end - start != 45 * 60 for start, end in zip(starts, ends)):
            raise ValueError("连堂课时长无效，请重新刷新课程。")
        if any(not 0 <= starts[i] - ends[i - 1] <= MAX_BREAK for i in range(1, len(ids))):
            raise ValueError("连堂课时间不连续，请重新刷新课程。")
        dates = {dt.datetime.fromtimestamp(v, ZONE).date() for v in (*starts, *ends)}
        if len(dates) != 1:
            raise ValueError("连堂课不能跨天，请重新刷新课程。")
        return cls(tuple(ids), tuple(starts), tuple(ends))

    def window(self, now):
        if now >= self.ends[-1] + LATE_END:
            return None
        if len(self.ids) == 3 and now >= self.starts[2] - EARLY_START:
            return self.ids[2], self.ends[-1] + LATE_END
        return self.ids[0], (self.starts[2] - EARLY_START if len(self.ids) == 3
                             else self.ends[-1] + LATE_END)

    def break_until(self, now):
        for end, start in zip(self.ends, self.starts[1:]):
            start -= EARLY_START
            if end <= now < start:
                return start
        return None

    def upcoming_start(self, now):
        """Use each cell's pre-roll, including the second cell sharing an ID."""
        return next((start for start in reversed(self.starts)
                     if now >= start - EARLY_START), self.starts[0])


class DeadlineStop:
    """A stream deadline stops the decoder without cancelling the whole class."""
    def __init__(self, parent, deadline, clock=None):
        self.parent = parent
        self.deadline = deadline
        self.clock = clock or time.time

    def is_set(self):
        return self.parent.is_set() or self.clock() >= self.deadline

    def set(self):
        self.parent.set()

    def wait(self, timeout=None):
        remaining = max(0.0, self.deadline - self.clock())
        self.parent.wait(remaining if timeout is None else min(max(0.0, timeout), remaining))
        return self.is_set()
