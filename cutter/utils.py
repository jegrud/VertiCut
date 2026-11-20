"""Мелкие помощники: время, имена файлов, номера сезонов и серий."""
from __future__ import annotations

import math
import re


def parse_time(text: str) -> float:
    """'1:23' -> 83, '1:02:03' -> 3723, '95' -> 95, '' -> 0."""
    t = (text or "").strip().replace(",", ".")
    if not t:
        return 0.0
    parts = t.split(":")
    if len(parts) > 3:
        raise ValueError(f"неверное время «{text}»")
    value = 0.0
    try:
        for p in parts:
            value = value * 60 + float(p)
    except ValueError:
        raise ValueError(f"неверное время «{text}» (пример: 1:30)") from None
    if value < 0 or not math.isfinite(value):
        raise ValueError(f"неверное время «{text}»")
    return value


def parse_interval(text: str) -> tuple[float, float] | None:
    """'1:30-2:15' -> (90, 135); пустая строка -> None."""
    t = (text or "").strip()
    if not t:
        return None
    pieces = re.split(r"\s*[-–—]\s*", t)
    if len(pieces) != 2:
        raise ValueError(f"неверный интервал «{text}» (пример: 1:30-2:15)")
    a, b = parse_time(pieces[0]), parse_time(pieces[1])
    if b <= a:
        raise ValueError(f"в интервале «{text}» конец раньше начала")
    return a, b


def fmt_time(sec: float) -> str:
    sec = max(0, int(round(sec)))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


_SEASON_EPISODE = [
    re.compile(r"[Ss](\d{1,2})\s*[._-]?\s*[EeЕе](\d{1,3})"),
    re.compile(r"(?<!\d)(\d{1,2})[xXхХ](\d{2,3})(?!\d)"),
    re.compile(r"(\d{1,2})\s*сезон\D{0,15}?(\d{1,3})\s*сери", re.I),
    re.compile(r"сезон\s*(\d{1,2})\D{0,15}?сери[яиюй]\s*(\d{1,3})", re.I),
    re.compile(r"season\s*(\d{1,2})\D{0,15}?episode\s*(\d{1,3})", re.I),
]
_EPISODE_ONLY = [
    re.compile(r"(\d{1,3})\s*сер(?:и|\.|\b)", re.I),   # «4 серия», «(04 сер.)»
    re.compile(r"сери[яиюй]\s*(\d{1,3})", re.I),
    re.compile(r"(?:episode|ep)\s*[._-]?\s*(\d{1,3})", re.I),
    re.compile(r"(?<![A-Za-z0-9])[Ee](\d{2,3})(?!\d)"),
]


def guess_season_episode(name: str) -> tuple[int | None, int | None]:
    for rx in _SEASON_EPISODE:
        m = rx.search(name)
        if m:
            return int(m.group(1)), int(m.group(2))
    for rx in _EPISODE_ONLY:
        m = rx.search(name)
        if m:
            return None, int(m.group(1))
    return None, None


def guess_series_name(stem: str) -> str:
    """Название сериала из имени файла: всё до номера серии, без мусора релиза."""
    cut = len(stem)
    for rx in _SEASON_EPISODE + _EPISODE_ONLY:
        m = rx.search(stem)
        if m:
            cut = min(cut, m.start())
    name = stem[:cut] if cut > 0 else stem
    name = re.sub(r"\[.*?\]|\(.*?\)", " ", name)
    name = re.sub(r"[._]+", " ", name)
    name = name.strip(" -–([{")
    return name or stem


def sanitize(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name)
    name = re.sub(r"\s+", "_", name.strip())
    return name.strip("._")[:80]


def natural_key(text: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", text)]
