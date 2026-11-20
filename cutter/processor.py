"""Обработка очереди: анализ серии, план нарезки и рендер частей с продолжением после сбоя."""
from __future__ import annotations

import hashlib
import os
import threading
import traceback
from dataclasses import asdict, dataclass
from pathlib import Path

from . import subtitles as subs
from .config import CACHE_DIR, OUT_H, QUEUE_FILE, WORK_DIR, Settings, read_json, write_json
from .ffmpeg_tools import (Cancelled, FFmpegError, MediaInfo, Runner, Stream, detect_bars,
                           extract_audio_wav, extract_subtitle_file, measure_loudness, probe,
                           scene_changes)
from .planner import content_ranges, plan
from .render import render_part, split_top_height
from .utils import fmt_time, guess_series_name, parse_interval, parse_time, sanitize


@dataclass
class Job:
    path: str
    season: int = 1
    episode: int = 1
    skip_start: str = "0:00"
    intro: str = ""
    skip_end: str = "0:00"
    state: str = "pending"   # pending | running | done | error
    message: str = ""

    @property
    def name(self) -> str:
        return Path(self.path).name


def load_queue() -> list[Job]:
    data = read_json(QUEUE_FILE, [])
    jobs = []
    for item in data if isinstance(data, list) else []:
        try:
            job = Job(**{k: v for k, v in item.items() if k in Job.__dataclass_fields__})
        except TypeError:
            continue
        if job.state == "running":
            job.state, job.message = "pending", "прервано"
        jobs.append(job)
    return jobs


def save_queue(jobs: list[Job]) -> None:
    write_json(QUEUE_FILE, [asdict(j) for j in jobs])


_cache_lock = threading.Lock()


class EpisodeCache:
    """Долгие результаты анализа серии (полосы, смены сцен, субтитры) — чтобы не считать заново."""

    def __init__(self, path: str):
        st = os.stat(path)
        ident = f"{os.path.abspath(path)}|{st.st_size}|{int(st.st_mtime)}"
        self.key = hashlib.sha1(ident.encode("utf-8")).hexdigest()[:16]
        self.file = CACHE_DIR / f"{self.key}.json"

    def get(self, name: str, default=None):
        with _cache_lock:
            data = read_json(self.file, {})
        return data.get(name, default)

    def set(self, name: str, value) -> None:
        with _cache_lock:
            data = read_json(self.file, {})
            data[name] = value
            write_json(self.file, data)


def output_dir(s: Settings, video_path: str) -> Path:
    return Path(s.output_dir) if s.output_dir.strip() else Path(video_path).parent / "TikTok"


def part_name(s: Settings, job: Job, k: int) -> str:
    series = sanitize(s.series_name.strip() or guess_series_name(Path(job.path).stem)) or "Serial"
    return f"{series}_S{job.season:02d}E{job.episode:02d}_part{k:02d}.mp4"


def get_bars(info: MediaInfo, cache: EpisodeCache):
    if cache.get("bars_checked"):
        return cache.get("bars")
    bars = detect_bars(info)
    cache.set("bars", bars)
    cache.set("bars_checked", True)
    return bars


def choose_audio(info: MediaInfo, lang: str) -> Stream | None:
    if not info.audio:
        return None
    if lang:
        hits = [a for a in info.audio if subs.lang_matches(f"{a.lang} {a.title}", lang)]
        if hits:
            return max(hits, key=lambda a: a.default)
    defaults = [a for a in info.audio if a.default]
    return defaults[0] if defaults else info.audio[0]


def load_cues(info: MediaInfo, s: Settings, out_dir: Path, cache: EpisodeCache, runner: Runner,
              log, on_progress=None, allow_whisper=True) -> tuple[list[subs.Cue], str]:
    """Субтитры серии по выбранному источнику. Возвращает (фразы, откуда взяты)."""
    mode = s.sub_source
    if mode == "none":
        return [], "отключены"

    if mode in ("auto", "external"):
        f = subs.find_external(info.path, s.sub_lang, [out_dir / "_subtitles"])
        if f:
            return subs.read_sub_file(f), f"файл «{f.name}»"
        if mode == "external":
            log("  ! рядом с видео нет файла субтитров")
            return [], "нет"

    if mode in ("auto", "embedded"):
        st = subs.choose_sub_stream(info, s.sub_lang)
        if st:
            key = f"embedded_{st.index}"
            raw = cache.get(key)
            if raw is None:
                tmp = WORK_DIR / f"{cache.key}_{st.index}.srt"
                try:
                    extract_subtitle_file(runner, info, st, tmp)
                    cues = subs.parse_srt(tmp.read_text(encoding="utf-8", errors="replace"))
                finally:
                    tmp.unlink(missing_ok=True)
                cache.set(key, [[c.start, c.end, c.text] for c in cues])
            else:
                cues = [subs.Cue(*c) for c in raw]
            return cues, f"встроенная дорожка {st.describe()}"
        if mode == "embedded":
            if any(x.codec not in subs.TEXT_SUB_CODECS for x in info.subs):
                log("  ! встроенные субтитры — картинки (PGS/VobSub), их нельзя перерисовать своим шрифтом")
            else:
                log("  ! в видео нет встроенных субтитров на нужном языке")
            return [], "нет"

    if mode in ("auto", "whisper") and allow_whisper:
        if not subs.whisper_available():
            log("  ! субтитры не найдены, а распознавание речи не установлено (запустите install_whisper.bat)")
            return [], "нет"
        audio = choose_audio(info, s.audio_lang)
        if audio is None:
            return [], "нет звука"
        wav = WORK_DIR / f"{cache.key}.wav"
        try:
            log(f"  распознаю речь (модель {s.whisper_model}), это может занять долгое время…")
            extract_audio_wav(runner, info, audio, wav)
            cues = subs.transcribe(str(wav), s.whisper_model, s.whisper_lang,
                                   s.style.max_chars * s.style.max_lines, info.duration, runner,
                                   on_progress or (lambda f: None), log)
        finally:
            wav.unlink(missing_ok=True)
        target = out_dir / "_subtitles" / f"{Path(info.path).stem}.srt"
        target.parent.mkdir(parents=True, exist_ok=True)
        subs.write_srt(cues, target)
        log(f"  субтитры сохранены в {target} — их можно поправить и запустить нарезку заново")
        return cues, "распознавание речи"
    return [], "нет"


class Processor:
    """Обрабатывает очередь серий. Колбэки вызываются из рабочего потока."""

    def __init__(self, settings: Settings, log, progress, job_changed):
        self.s = settings
        self.log = log
        self.progress = progress
        self.job_changed = job_changed
        self.runner = Runner()

    def cancel(self) -> None:
        self.runner.cancel()

    def run(self, jobs: list[Job]) -> None:
        todo = [j for j in jobs if j.state != "done"]
        for n, job in enumerate(todo):
            if self.runner.cancelled:
                break
            job.state, job.message = "running", "анализ…"
            self.job_changed(job)
            try:
                count = self._process(job, n, len(todo))
                job.state, job.message = "done", f"готово, частей: {count}"
            except Cancelled:
                job.state, job.message = "pending", "остановлено"
                self.job_changed(job)
                self.log("■ Остановлено пользователем")
                break
            except (FFmpegError, ValueError, OSError) as e:
                job.state, job.message = "error", "ошибка: " + str(e).splitlines()[0][:200]
                self.log(f"✗ {job.name}: {e}")
            except Exception as e:
                job.state, job.message = "error", f"ошибка: {e}"[:200]
                self.log(f"✗ {job.name}:\n{traceback.format_exc()}")
            self.job_changed(job)

    def _process(self, job: Job, n: int, total: int) -> int:
        s = self.s

        def report(frac, text):
            job.message = text
            self.progress((n + frac) / total, f"Серия {n + 1} из {total} ({job.name}): {text}")
            self.job_changed(job)

        info = probe(job.path)
        bottom = None
        if s.bottom_video:
            try:
                bottom = probe(s.bottom_video)
            except FFmpegError as e:
                raise FFmpegError(f"видео под сериалом «{Path(s.bottom_video).name}»: {e}") from e
        subtitle_height = split_top_height(s.aspect) if bottom else OUT_H
        ranges = content_ranges(info.duration, parse_time(job.skip_start),
                                parse_interval(job.intro), parse_time(job.skip_end))
        if not ranges:
            raise ValueError("после пропуска начала и конца ничего не осталось")
        cache = EpisodeCache(job.path)
        out_dir = output_dir(s, job.path)
        out_dir.mkdir(parents=True, exist_ok=True)
        self.log(f"▶ {job.name} — {fmt_time(info.duration)}, {info.width}×{info.height}, "
                 f"{float(info.fps):.3f} к/с")
        if bottom:
            self.log(f"  видео под сериалом: {Path(bottom.path).name} (без звука)")

        bars = None
        if s.remove_bars:
            report(0.0, "поиск чёрных полос")
            bars = get_bars(info, cache)
            if bars:
                self.log(f"  чёрные полосы обрезаны, картинка {bars[0]}×{bars[1]}")
        audio = choose_audio(info, s.audio_lang)
        self.log("  звук: " + (audio.describe() if audio else "нет"))

        report(0.01, "субтитры")
        cues, src = load_cues(info, s, out_dir, cache, self.runner, self.log,
                              on_progress=lambda f: report(0.02 + 0.3 * f, f"распознавание речи {f:.0%}"))
        self.log(f"  субтитры: {src}, фраз: {len(cues)}")

        key = f"scenes_{s.scene_threshold}"
        scenes = cache.get(key)
        if scenes is None:
            scenes = scene_changes(self.runner, info, s.scene_threshold,
                                   lambda f: report(0.32 + 0.08 * f, f"поиск смены сцен {f:.0%}"))
            cache.set(key, scenes)
        self.log(f"  смен сцен: {len(scenes)}")

        parts = plan(ranges, cues, scenes, s)
        if not parts:
            raise ValueError("не получилось выделить ни одной части")
        self.log(f"  частей: {len(parts)}, длительность: " + ", ".join(f"{p.dur:.0f}" for p in parts) + " с")

        laid = subs.layout_cues(cues, s.style) if cues else []
        total_dur = sum(p.dur for p in parts)
        done = 0.0
        for k, part in enumerate(parts, 1):
            out = out_dir / part_name(s, job, k)
            base = 0.4 + 0.6 * done / total_dur
            weight = 0.6 * part.dur / total_dur
            done += part.dur
            if out.exists() and not s.overwrite:
                self.log(f"  = {out.name} уже есть, пропускаю")
                continue
            label = f"часть {k} из {len(parts)}"
            report(base, label)

            ass_file = ass_rel = None
            events = subs.events_for_part(laid, part.start, part.end)
            if events:
                ass_file = WORK_DIR / f"{cache.key}_{k:02d}.ass"
                subs.write_ass(ass_file, events, s.style, position_height=subtitle_height)
                ass_rel = f"data/work/{ass_file.name}"
            loud = None
            if s.normalize and audio is not None:
                loud = measure_loudness(self.runner, info, audio, part.start, part.dur, s.target_lufs)
            try:
                render_part(self.runner, info, s, bars, audio, part.start, part.dur, ass_rel, out, loud,
                            lambda f, b=base, w=weight, t=label: report(b + w * f, f"{t} — {f:.0%}"),
                            bottom=bottom)
            finally:
                if ass_file:
                    ass_file.unlink(missing_ok=True)
            self.log(f"  ✓ {out.name}  [{fmt_time(part.start)}–{fmt_time(part.end)}, {part.dur:.0f} с]")
        return len(parts)
