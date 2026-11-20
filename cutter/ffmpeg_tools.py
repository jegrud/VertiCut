"""Обёртки над ffmpeg/ffprobe: поиск программ, разбор файла, запуск с прогрессом и отменой."""
from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import threading
from collections import deque
from dataclasses import dataclass, field
from fractions import Fraction
from functools import lru_cache
from pathlib import Path

from .config import APP_DIR
from .utils import parse_time

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class FFmpegError(RuntimeError):
    """Ошибка запуска или работы ffmpeg."""


class Cancelled(Exception):
    """Пользователь нажал «Стоп»."""


@lru_cache(maxsize=None)
def tool_path(name: str) -> str:
    for cand in (APP_DIR / "ffmpeg" / "bin" / f"{name}.exe", APP_DIR / "ffmpeg" / f"{name}.exe"):
        if cand.is_file():
            return str(cand)
    found = shutil.which(name)
    if not found:
        raise FFmpegError(
            f"Не найден {name}. Установите FFmpeg командой «winget install Gyan.FFmpeg» "
            f"или положите {name}.exe в папку ffmpeg рядом с программой."
        )
    return found


def run_quiet(args, timeout=None, cwd=None) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, capture_output=True, stdin=subprocess.DEVNULL,
        creationflags=NO_WINDOW, timeout=timeout, cwd=cwd,
    )


@dataclass
class Stream:
    index: int
    codec: str
    lang: str = ""
    title: str = ""
    default: bool = False
    forced: bool = False
    channels: int = 0

    def describe(self) -> str:
        bits = [f"№{self.index}", self.codec]
        if self.lang:
            bits.append(self.lang)
        if self.title:
            bits.append(f"«{self.title}»")
        return " ".join(bits)


@dataclass
class MediaInfo:
    path: str
    duration: float
    video: Stream
    width: int
    height: int
    sar: Fraction
    fps: Fraction
    interlaced: bool
    audio: list[Stream] = field(default_factory=list)
    subs: list[Stream] = field(default_factory=list)
    video_duration: float | None = None

    @property
    def fps_str(self) -> str:
        return f"{self.fps.numerator}/{self.fps.denominator}"


def _fraction(value, sep="/"):
    try:
        num, den = str(value).split(sep)
        f = Fraction(int(num), int(den))
        return f if f > 0 else None
    except (ValueError, ZeroDivisionError):
        return None


def _positive(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v > 0 else None


def probe(path: str) -> MediaInfo:
    res = run_quiet([tool_path("ffprobe"), "-v", "error", "-print_format", "json",
                     "-show_format", "-show_streams", str(path)], timeout=120)
    if res.returncode != 0:
        err = res.stderr.decode("utf-8", "replace").strip()
        raise FFmpegError(f"не удалось прочитать файл «{Path(path).name}»: {err}")
    data = json.loads(res.stdout.decode("utf-8", "replace") or "{}")

    video = vraw = None
    audio, subs = [], []
    for st in data.get("streams", []):
        disp = st.get("disposition") or {}
        tags = {k.lower(): v for k, v in (st.get("tags") or {}).items()}
        s = Stream(
            index=int(st["index"]),
            codec=st.get("codec_name", ""),
            lang=(tags.get("language") or "").lower(),
            title=tags.get("title") or "",
            default=bool(disp.get("default")),
            forced=bool(disp.get("forced")),
            channels=int(st.get("channels") or 0),
        )
        kind = st.get("codec_type")
        if kind == "video" and not disp.get("attached_pic") and video is None:
            video, vraw = s, st
        elif kind == "audio":
            audio.append(s)
        elif kind == "subtitle":
            subs.append(s)
    if video is None:
        raise FFmpegError("в файле нет видеодорожки")

    video_duration = _positive(vraw.get("duration"))
    if video_duration is None:
        tags = {k.lower(): v for k, v in (vraw.get("tags") or {}).items()}
        try:
            video_duration = parse_time(tags.get("duration", "")) or None
        except ValueError:
            pass
    duration = _positive(data.get("format", {}).get("duration")) or video_duration
    if not duration:
        raise FFmpegError("не удалось определить длительность видео")

    fps = None
    for key in ("avg_frame_rate", "r_frame_rate"):
        f = _fraction(vraw.get(key))
        if f and 5 <= f <= 121:
            fps = f.limit_denominator(1001)
            break

    return MediaInfo(
        path=str(path),
        duration=duration,
        video=video,
        width=int(vraw.get("width") or 0),
        height=int(vraw.get("height") or 0),
        sar=_fraction(vraw.get("sample_aspect_ratio"), ":") or Fraction(1),
        fps=fps or Fraction(25),
        interlaced=vraw.get("field_order") in ("tt", "bb", "tb", "bt"),
        audio=audio,
        subs=subs,
        video_duration=video_duration,
    )


_PROGRESS_LINE = re.compile(r"^[a-z_0-9]+=")


class Runner:
    """Запускает ffmpeg и позволяет прервать текущий процесс из другого потока."""

    def __init__(self):
        self._lock = threading.Lock()
        self._proc = None
        self._cancel = threading.Event()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def cancel(self) -> None:
        self._cancel.set()
        with self._lock:
            if self._proc and self._proc.poll() is None:
                self._proc.kill()

    def check(self) -> None:
        if self.cancelled:
            raise Cancelled()

    def ffmpeg(self, args, *, total=None, on_progress=None, on_line=None, cwd=None) -> list[str]:
        """Запускает ffmpeg; on_progress получает долю 0..1 от total секунд."""
        self.check()
        cmd = [tool_path("ffmpeg"), "-hide_banner", "-nostdin", "-nostats", "-progress", "pipe:1", *args]
        tail = deque(maxlen=25)
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            cwd=cwd, text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW,
        )
        with self._lock:
            self._proc = proc
        if self.cancelled:
            proc.kill()
        try:
            for raw in proc.stdout:
                line = raw.rstrip()
                if _PROGRESS_LINE.match(line):
                    if line.startswith("out_time_us=") and total and on_progress:
                        try:
                            on_progress(min(1.0, max(0.0, int(line[12:]) / 1e6 / total)))
                        except ValueError:
                            pass
                    continue
                if not line:
                    continue
                tail.append(line)
                if on_line:
                    on_line(line)
        finally:
            proc.wait()
            with self._lock:
                self._proc = None
        if self.cancelled:
            raise Cancelled()
        if proc.returncode != 0:
            raise FFmpegError("FFmpeg завершился с ошибкой:\n" + "\n".join(tail))
        return list(tail)


_CROP_RE = re.compile(r"crop=(\d+):(\d+):(\d+):(\d+)")


def detect_bars(info: MediaInfo):
    """Ищет чёрные полосы, вшитые в картинку. Возвращает [w, h, x, y] или None."""
    W, H = info.width, info.height
    boxes = []
    for frac in (0.12, 0.27, 0.42, 0.57, 0.72, 0.87):
        t = info.duration * frac
        try:
            res = run_quiet([
                tool_path("ffmpeg"), "-hide_banner", "-nostdin", "-ss", f"{t:.2f}", "-i", info.path,
                "-map", f"0:{info.video.index}", "-frames:v", "20",
                "-vf", "cropdetect=limit=0.1:round=2:reset=0", "-an", "-sn", "-dn", "-f", "null", "-",
            ], timeout=90)
        except subprocess.TimeoutExpired:
            continue
        found = _CROP_RE.findall(res.stderr.decode("utf-8", "replace"))
        if not found:
            continue
        w, h, x, y = map(int, found[-1])
        if w * h >= 0.3 * W * H:  # почти чёрный кадр даёт ложную рамку
            boxes.append((x, y, x + w, y + h))
    if len(boxes) < 2:
        return None
    x1 = min(b[0] for b in boxes)
    y1 = min(b[1] for b in boxes)
    x2 = max(b[2] for b in boxes)
    y2 = max(b[3] for b in boxes)
    x1 += x1 % 2
    y1 += y1 % 2
    w = (x2 - x1) // 2 * 2
    h = (y2 - y1) // 2 * 2
    if w <= 0 or h <= 0 or (w >= W * 0.97 and h >= H * 0.97):
        return None
    return [w, h, x1, y1]


_SCD_RE = re.compile(r"lavfi\.scd\.time:\s*([\d.]+)")


def scene_changes(runner: Runner, info: MediaInfo, threshold: float, on_progress=None) -> list[float]:
    """Моменты смены сцены (секунды от начала файла)."""
    times = []

    def on_line(line):
        m = _SCD_RE.search(line)
        if m:
            times.append(float(m.group(1)))

    runner.ffmpeg([
        "-i", info.path, "-map", f"0:{info.video.index}", "-an", "-sn", "-dn",
        "-vf", f"scale=320:-2:flags=fast_bilinear,scdet=threshold={threshold}",
        "-f", "null", "-",
    ], total=info.duration, on_progress=on_progress, on_line=on_line)
    return sorted({round(t, 3) for t in times})


_LOUDNORM_RE = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.S)


def measure_loudness(runner: Runner, info: MediaInfo, audio: Stream, start: float, dur: float, target: float):
    """Первый проход loudnorm: замер громкости куска. None — если кусок тихий."""
    lines = []
    runner.ffmpeg([
        "-ss", f"{start:.3f}", "-i", info.path, "-t", f"{dur:.3f}",
        "-map", f"0:{audio.index}", "-vn", "-sn", "-dn",
        "-af", f"aformat=channel_layouts=stereo,loudnorm=I={target}:TP=-1.5:LRA=11:print_format=json",
        "-f", "null", "-",
    ], on_line=lines.append)
    m = _LOUDNORM_RE.search("\n".join(lines))
    if not m:
        return None
    data = json.loads(m.group(0))
    for key in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset"):
        try:
            if not math.isfinite(float(data.get(key))):
                return None
        except (TypeError, ValueError):
            return None
    return data


def extract_audio_wav(runner: Runner, info: MediaInfo, audio: Stream, out: Path, on_progress=None) -> None:
    runner.ffmpeg([
        "-y", "-i", info.path, "-map", f"0:{audio.index}", "-vn", "-sn", "-dn",
        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out),
    ], total=info.duration, on_progress=on_progress)


def extract_subtitle_file(runner: Runner, info: MediaInfo, stream: Stream, out: Path) -> None:
    runner.ffmpeg(["-y", "-i", info.path, "-map", f"0:{stream.index}", "-c:s", "srt", "-f", "srt", str(out)])


def frame_seek_time(info: MediaInfo, t: float, *, loop: bool = False) -> float:
    if loop:
        duration = info.video_duration or info.duration
        return min(t % duration, max(0.0, duration - 1 / float(info.fps)))
    return min(max(0.0, t), max(0.0, info.duration - 1.0))


def grab_frame(info: MediaInfo, t: float, filter_args: list[str], cwd=None, loglevel="error",
               *, extra_inputs: tuple[str, ...] = (), loop: bool = False) -> tuple[bytes, str]:
    """Один кадр в PNG. filter_args — ['-vf', ...] или ['-filter_complex', ..., '-map', '[vout]']."""
    t = frame_seek_time(info, t, loop=loop)
    res = run_quiet([
        tool_path("ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", loglevel,
        "-ss", f"{t:.3f}", "-i", info.path, *extra_inputs, *filter_args,
        "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "image2pipe", "-c:v", "png", "-",
    ], timeout=120, cwd=cwd)
    log = res.stderr.decode("utf-8", "replace")
    if res.returncode != 0 or not res.stdout:
        raise FFmpegError(log.strip()[-800:] or "не удалось получить кадр")
    return res.stdout, log
