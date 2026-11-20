"""Сборка команды FFmpeg для одной части: обрезка по центру, фон, субтитры, кодирование."""
from __future__ import annotations

import os
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from .config import APP_DIR, ASPECTS, OUT_H, OUT_W, Settings
from .ffmpeg_tools import MediaInfo, Runner, Stream, run_quiet, tool_path

ENCODERS = {
    "libx264": "x264 — процессор (лучшее качество, медленнее)",
    "h264_amf": "AMD AMF — видеокарта AMD (быстрее)",
    "h264_nvenc": "NVIDIA NVENC — видеокарта NVIDIA (быстрее)",
    "h264_qsv": "Intel Quick Sync — графика Intel (быстрее)",
}


def _even(x) -> int:
    return max(2, int(x) // 2 * 2)


@dataclass
class Geometry:
    pre: list[str]   # фильтры до обрезки: деинтерлейс, чёрные полосы, квадратные пиксели
    crop_w: int
    crop_h: int
    fg_h: int        # высота картинки в ролике (ширина всегда OUT_W)

    @property
    def full_screen(self) -> bool:
        return self.fg_h >= OUT_H


def split_top_height(aspect: str) -> int:
    aw, ah = ASPECTS.get(aspect, (4, 3))
    return min(OUT_H // 2, _even(Fraction(OUT_W * ah, aw)))


def geometry(info: MediaInfo, bars, aspect: str, *, max_height: int | None = None) -> Geometry:
    pre = []
    if info.interlaced:
        pre.append("bwdif=mode=send_frame:deint=interlaced")
    w, h = info.width, info.height
    if bars:
        bw, bh, bx, by = bars
        pre.append(f"crop={bw}:{bh}:{bx}:{by}")
        w, h = bw, bh
    if info.sar != 1:
        w = _even(w * info.sar)
        pre.append(f"scale={w}:{h},setsar=1")
    aw, ah = ASPECTS.get(aspect, (4, 3))
    r = Fraction(aw, ah)
    if max_height is not None:
        r = max(r, Fraction(OUT_W, max_height))
    if Fraction(w, h) > r:
        cw, ch = _even(h * r), _even(h)
    else:
        cw, ch = _even(w), _even(w / r)
    return Geometry(pre, cw, ch, min(OUT_H, _even(OUT_W / r)))


def bottom_video_filter(info: MediaInfo, height: int) -> str:
    filters = []
    if info.interlaced:
        filters.append("bwdif=mode=send_frame:deint=interlaced")
    if info.sar != 1:
        filters.append("scale=max(2\\,trunc(iw*sar/2)*2):ih,setsar=1")
    filters += [
        f"scale={OUT_W}:{height}:force_original_aspect_ratio=increase:force_divisible_by=2:flags=lanczos",
        f"crop={OUT_W}:{height}",
        "setsar=1",
    ]
    return ",".join(filters)


def build_filter(info: MediaInfo, s: Settings, bars, ass_rel: str | None = None, *,
                 bottom: MediaInfo | None = None) -> str:
    """filter_complex с выходом [vout]. ass_rel — путь к .ass относительно папки программы."""
    g = geometry(info, bars if s.remove_bars else None, s.aspect,
                 max_height=OUT_H // 2 if bottom is not None else None)
    src = f"[0:{info.video.index}]"
    pre = "".join(f + "," for f in g.pre)
    fg = (f"crop={g.crop_w}:{g.crop_h}:(iw-{g.crop_w})/2:(ih-{g.crop_h})/2,"
          f"scale={OUT_W}:{g.fg_h}:flags=lanczos,setsar=1")
    tail = (f"ass=filename={ass_rel}:fontsdir=fonts," if ass_rel else "") + "format=yuv420p"
    if bottom is not None:
        lower = bottom_video_filter(bottom, OUT_H - g.fg_h)
        return (
            f"{src}{pre}{fg},pad={OUT_W}:{OUT_H}:0:0,format=yuv420p[top];"
            f"[1:{bottom.video.index}]{lower},setpts=PTS-STARTPTS,format=yuv420p[bottom];"
            f"[top][bottom]overlay=0:{g.fg_h},{tail}[vout]"
        )
    if g.full_screen:
        return f"{src}{pre}{fg},{tail}[vout]"
    if s.background == "blur":
        return (
            f"{src}{pre}split=2[fgsrc][bgsrc];"
            f"[fgsrc]{fg}[fg];"
            f"[bgsrc]scale=270:480:force_original_aspect_ratio=increase:force_divisible_by=2:flags=bilinear,"
            f"crop=270:480,boxblur=luma_radius=12:luma_power=3,scale={OUT_W}:{OUT_H}:flags=bicubic,"
            f"eq=brightness=-0.07,setsar=1[bg];"
            f"[bg][fg]overlay=x=(W-w)/2:y=(H-h)/2,{tail}[vout]"
        )
    return f"{src}{pre}{fg},pad={OUT_W}:{OUT_H}:(ow-iw)/2:(oh-ih)/2:color=black,{tail}[vout]"


def video_args(s: Settings, fps: str) -> list[str]:
    mx, buf = f"{s.max_bitrate}M", f"{s.max_bitrate * 2}M"
    br = f"{min(s.bitrate, s.max_bitrate)}M"
    crf = s.quality_mode == "crf"
    if s.encoder == "h264_nvenc":
        a = ["-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq", "-profile:v", "high",
             "-rc", "vbr", "-pix_fmt", "yuv420p"]
        a += ["-cq", str(s.crf + 2), "-b:v", "0"] if crf else ["-b:v", br]
        a += ["-maxrate", mx, "-bufsize", buf]
    elif s.encoder == "h264_amf":
        a = ["-c:v", "h264_amf", "-usage", "transcoding", "-quality", "quality",
             "-profile:v", "high", "-pix_fmt", "yuv420p"]
        if crf:
            a += ["-rc", "cqp", "-qp_i", str(s.crf), "-qp_p", str(s.crf + 2), "-qp_b", str(s.crf + 4)]
        else:
            a += ["-rc", "vbr_peak", "-b:v", br, "-maxrate", mx, "-bufsize", buf]
    elif s.encoder == "h264_qsv":
        a = ["-c:v", "h264_qsv", "-preset", "slower", "-profile:v", "high", "-pix_fmt", "nv12"]
        a += ["-global_quality", str(s.crf + 3)] if crf else ["-b:v", br, "-maxrate", mx, "-bufsize", buf]
    else:
        a = ["-c:v", "libx264", "-preset", s.x264_preset, "-profile:v", "high", "-pix_fmt", "yuv420p"]
        a += ["-crf", str(s.crf)] if crf else ["-b:v", br]
        a += ["-maxrate", mx, "-bufsize", buf]
    return a + ["-r", fps, "-fps_mode", "cfr"]


def audio_filter(loud: dict | None, target: float) -> str:
    chain = ["aformat=channel_layouts=stereo"]
    if loud:
        chain.append(
            "loudnorm=I={t}:TP=-1.5:LRA=11:measured_I={input_i}:measured_TP={input_tp}:"
            "measured_LRA={input_lra}:measured_thresh={input_thresh}:offset={target_offset}:"
            "linear=true".format(t=target, **loud)
        )
    chain.append("aresample=48000")
    return ",".join(chain)


def render_part(runner: Runner, info: MediaInfo, s: Settings, bars, audio: Stream | None,
                start: float, dur: float, ass_rel: str | None, out_path: Path,
                loud: dict | None, on_progress=None, *, bottom: MediaInfo | None = None) -> None:
    tmp = out_path.with_name("~" + out_path.name)
    args = ["-y", "-ss", f"{start:.3f}", "-i", info.path]
    if bottom is not None:
        args += ["-stream_loop", "-1", "-an", "-sn", "-dn", "-i", bottom.path]
    args += ["-t", f"{dur:.3f}", "-filter_complex", build_filter(info, s, bars, ass_rel, bottom=bottom),
             "-map", "[vout]"]
    if audio is not None:
        args += ["-map", f"0:{audio.index}", "-af", audio_filter(loud, s.target_lufs),
                 "-c:a", "aac", "-b:a", "320k", "-ar", "48000"]
    args += video_args(s, info.fps_str)
    args += ["-map_metadata", "-1", "-map_chapters", "-1", "-max_muxing_queue_size", "4096",
             "-movflags", "+faststart", "-f", "mp4", str(tmp)]
    try:
        runner.ffmpeg(args, total=dur, on_progress=on_progress, cwd=str(APP_DIR))
        os.replace(tmp, out_path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def detect_encoders() -> list[str]:
    """Какие кодировщики H.264 реально работают на этом компьютере (пробное кодирование)."""
    found = []
    for enc in ENCODERS:
        cmd = [tool_path("ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", "error",
               "-f", "lavfi", "-i", f"color=c=gray:s={OUT_W}x{OUT_H}:r=30", "-frames:v", "8",
               *video_args(Settings(encoder=enc), "30"), "-f", "null", "-"]
        try:
            if run_quiet(cmd, timeout=60).returncode == 0:
                found.append(enc)
        except Exception:
            pass
    return found
