"""Выбор точек разреза: части нужной длины, разрез на паузе в речи и/или смене сцены.

Каждая возможная точка разреза получает оценку:
  ~3   смена сцены во время паузы в речи (лучший вариант)
  ~2   середина паузы между фразами
   1   любой момент тишины
   0.5 смена сцены посреди фразы
   0   посреди фразы (только если иначе никак)
Затем динамическим программированием подбирается набор частей длиной min..max,
у которого сумма «плохости» разрезов и отклонений от целевой длины минимальна.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass

GRID_STEP = 0.5          # шаг запасных точек разреза, с
SPEECH_MARGIN = 0.12     # не резать ближе этого к началу/концу фразы, с
MIN_KEEP = 15.0          # отдельный кусок короче этого не сохраняется
AUTO_MERGE_LIMIT = 30.0  # режим «авто»: остаток короче — приклеить к последней части
TINY_TAIL = 5.0          # остаток короче — приклеивается всегда


@dataclass
class Part:
    start: float
    end: float

    @property
    def dur(self) -> float:
        return self.end - self.start


def content_ranges(duration, skip_start, intro, skip_end) -> list[tuple[float, float]]:
    """Куски серии, которые идут в нарезку: без начала, титров и заставки."""
    a, b = max(0.0, skip_start), duration - max(0.0, skip_end)
    if b - a < 1:
        return []
    ranges = [(a, b)]
    if intro:
        i0, i1 = intro
        new = []
        for x, y in ranges:
            if i1 <= x or i0 >= y:
                new.append((x, y))
                continue
            if i0 > x:
                new.append((x, i0))
            if i1 < y:
                new.append((i1, y))
        ranges = [(x, y) for x, y in new if y - x >= 1]
    return ranges


class _Speech:
    """Интервалы речи по субтитрам (пересекающиеся фразы склеены)."""

    def __init__(self, cues):
        merged: list[list[float]] = []
        for c in sorted(cues, key=lambda c: c.start):
            if merged and c.start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], c.end)
            else:
                merged.append([c.start, c.end])
        self.iv = merged
        self.starts = [m[0] for m in merged]

    def gap_at(self, t: float, a: float, b: float):
        """Длина паузы вокруг момента t или None, если в этот момент говорят."""
        i = bisect.bisect_right(self.starts, t) - 1
        if i >= 0 and t < self.iv[i][1] + SPEECH_MARGIN:
            return None
        if i + 1 < len(self.iv) and t > self.iv[i + 1][0] - SPEECH_MARGIN:
            return None
        left = self.iv[i][1] if i >= 0 else a
        right = self.iv[i + 1][0] if i + 1 < len(self.iv) else b
        return max(0.0, right - left)


def _candidates(a, b, speech: _Speech, scenes, has_speech: bool) -> dict[float, float]:
    cands: dict[float, float] = {}

    def add(t, score):
        if a < t < b:
            key = round(t, 3)
            if score > cands.get(key, -1.0):
                cands[key] = score

    lo = bisect.bisect_left(scenes, a)
    hi = bisect.bisect_right(scenes, b)
    for t in scenes[lo:hi]:
        if has_speech:
            gap = speech.gap_at(t, a, b)
            add(t, 0.5 if gap is None else 3.0 + min(gap, 2.0) / 4)
        else:
            add(t, 3.0)

    if has_speech:
        iv = speech.iv
        for (_, e0), (s1, _) in zip(iv, iv[1:]):
            gap = s1 - e0
            if gap >= 0.25:
                add((e0 + s1) / 2, 2.0 + min(gap, 2.0) / 4)

    t = a + GRID_STEP
    while t < b:
        if has_speech:
            add(t, 0.0 if speech.gap_at(t, a, b) is None else 1.0)
        else:
            add(t, 1.0)
        t += GRID_STEP
    return cands


def _plan_range(a, b, cands, s) -> list[Part]:
    pts = sorted(cands)
    pts.append(b)
    score = dict(cands)
    score[b] = 4.0
    nodes = [a] + pts
    n = len(nodes)
    # начало части, если предыдущая закончилась в узле i (с повтором overlap секунд)
    starts = [a] + [p - s.overlap for p in pts]
    inf = float("inf")
    cost = [inf] * n
    prev = [-1] * n
    cost[0] = 0.0
    span = max(1.0, s.max_len - s.min_len)

    for j in range(1, n):
        t = nodes[j]
        lo, hi = t - s.max_len, t - s.min_len
        best, arg = inf, -1
        if lo <= a <= hi:
            best, arg = abs(t - a - s.target_len) / span, 0
        i0 = bisect.bisect_left(starts, lo, 1, j)
        i1 = bisect.bisect_right(starts, hi, 1, j)
        for i in range(i0, i1):
            if cost[i] == inf:
                continue
            c = cost[i] + abs(t - starts[i] - s.target_len) / span
            if c < best:
                best, arg = c, i
        if arg >= 0:
            cost[j] = best + (4.0 - score[t])
            prev[j] = arg

    def backtrack(j):
        ends = []
        while j > 0:
            ends.append(j)
            j = prev[j]
        parts, start = [], a
        for j in reversed(ends):
            parts.append(Part(start, nodes[j]))
            start = nodes[j] - s.overlap
        return parts

    if cost[n - 1] < inf:
        return backtrack(n - 1)

    # Ровно поделить нельзя — режем сколько получается, остаток по настройке
    reach = [j for j in range(1, n) if cost[j] < inf]
    if not reach:
        return [Part(a, b)] if b - a >= MIN_KEEP and s.tail_policy != "drop" else []
    far = max(nodes[j] for j in reach)
    j = min((k for k in reach if nodes[k] >= far - 10), key=lambda k: cost[k])
    parts = backtrack(j)
    tail_start = nodes[j] - s.overlap
    tail = b - tail_start
    if s.tail_policy == "drop":
        return parts
    if s.tail_policy == "merge" or tail < TINY_TAIL or (s.tail_policy == "auto" and tail < AUTO_MERGE_LIMIT):
        parts[-1].end = b
    else:
        parts.append(Part(tail_start, b))
    return parts


def plan(ranges, cues, scenes, s) -> list[Part]:
    speech = _Speech(cues)
    scenes = sorted(scenes)
    parts: list[Part] = []
    for a, b in ranges:
        parts += _plan_range(a, b, _candidates(a, b, speech, scenes, bool(cues)), s)
    return parts
