"""Субтитры: поиск, чтение, распознавание речи, разбивка на строки и запись в ASS."""
from __future__ import annotations

import importlib.util
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .config import OUT_H, OUT_W, SubtitleStyle
from .ffmpeg_tools import Cancelled, MediaInfo, Stream, run_quiet, tool_path

SUB_EXTS = (".srt", ".ass", ".ssa", ".vtt")
TEXT_SUB_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}
SIGNS_WORDS = ("forced", "sign", "надпис")

LANG_GROUPS = [
    {"rus", "ru", "russian", "рус", "русские", "русский"},
    {"eng", "en", "english", "англ"},
    {"ukr", "uk", "ua", "ukrainian", "укр"},
    {"kir", "ky", "kyrgyz", "кыр"},
    {"kaz", "kk", "kazakh", "каз"},
    {"uzb", "uz", "uzbek"},
    {"tur", "tr", "turkish"},
    {"ger", "deu", "de", "german"},
    {"fre", "fra", "fr", "french"},
    {"spa", "es", "spanish"},
    {"ita", "it", "italian"},
    {"jpn", "ja", "jp", "japanese"},
    {"kor", "ko", "korean"},
    {"chi", "zho", "zh", "chinese"},
]


@dataclass
class Cue:
    start: float
    end: float
    text: str


# ---------- языки ----------

def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-zа-яё]+", text.lower()))


def _group(code: str) -> set[str]:
    code = (code or "").lower().strip()
    for g in LANG_GROUPS:
        if code in g:
            return g
    return {code} if code else set()


def lang_matches(text: str, pref: str) -> bool:
    return bool(_tokens(text) & _group(pref))


def _other_lang(text: str, pref: str) -> bool:
    own, toks = _group(pref), _tokens(text)
    return any(toks & g for g in LANG_GROUPS if g is not own)


# ---------- чтение ----------

_TS_RE = re.compile(
    r"(\d+):(\d{1,2}):(\d{1,2})[,.](\d{1,3})\s*-->\s*(\d+):(\d{1,2}):(\d{1,2})[,.](\d{1,3})"
)
_TAG_RE = re.compile(r"<[^>]*>|\{[^}]*\}")


def _ts(h, m, s, ms) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def clean_text(text: str) -> str:
    text = text.replace("\\N", " ").replace("\\n", " ").replace("\\h", " ")
    text = _TAG_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    if re.match(r"^m\s+-?\d", text):  # векторные рисунки из ASS
        return ""
    return text


def parse_srt(text: str) -> list[Cue]:
    cues: list[Cue] = []
    cur = None
    buf: list[str] = []
    collecting = False

    def flush():
        if cur and buf:
            t = clean_text(" ".join(buf))
            if t and cur[1] > cur[0]:
                cues.append(Cue(cur[0], cur[1], t))

    for line in text.splitlines():
        m = _TS_RE.search(line)
        if m:
            if buf and buf[-1].strip().isdigit():  # номер следующего блока без пустой строки
                buf.pop()
            flush()
            g = m.groups()
            cur, buf, collecting = (_ts(*g[:4]), _ts(*g[4:])), [], True
        elif collecting:
            if line.strip():
                buf.append(line.strip())
            else:
                collecting = False
    flush()
    cues.sort(key=lambda c: c.start)
    return cues


def read_text_auto(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def read_sub_file(path: Path) -> list[Cue]:
    if path.suffix.lower() == ".srt":
        return parse_srt(read_text_auto(path))
    res = run_quiet([tool_path("ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", "error",
                     "-i", str(path), "-f", "srt", "-"], timeout=120)
    if res.returncode != 0:
        raise RuntimeError(f"не удалось прочитать субтитры «{path.name}»: "
                           + res.stderr.decode("utf-8", "replace").strip()[-300:])
    return parse_srt(res.stdout.decode("utf-8", "replace"))


def write_srt(cues: list[Cue], path: Path) -> None:
    def ts(t):
        ms = int(round(t * 1000))
        h, ms = divmod(ms, 3600000)
        m, ms = divmod(ms, 60000)
        s, ms = divmod(ms, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    blocks = [f"{i}\n{ts(c.start)} --> {ts(c.end)}\n{c.text}\n" for i, c in enumerate(cues, 1)]
    path.write_text("\n".join(blocks), encoding="utf-8")


def find_external(video_path: str, lang: str, priority_dirs=()) -> Path | None:
    """Файл субтитров рядом с видео: Name.srt, Name.rus.srt, Subs/Name.srt, Subs/Name/2_Russian.srt…"""
    video = Path(video_path)
    stem = video.stem.lower()
    best = None

    def consider(f: Path, extra: str, bonus: int, any_name=False):
        nonlocal best
        if f.suffix.lower() not in SUB_EXTS:
            return
        fs = f.stem.lower()
        if any_name:
            tags = fs + " " + extra
        elif fs.startswith(stem):
            tags = fs[len(stem):] + " " + extra
        else:
            return
        if any(w in tags for w in SIGNS_WORDS):
            return
        if lang and lang_matches(tags, lang):
            score = 3
        elif lang and _other_lang(tags, lang):
            return
        else:
            score = 2
        score += bonus
        if best is None or score > best[0]:
            best = (score, f)

    def files(folder: Path):
        try:
            return [f for f in folder.iterdir() if f.is_file()]
        except OSError:
            return []

    for d in priority_dirs:
        for f in files(Path(d)):
            consider(f, "", 10)

    folders = [(video.parent, "", False)]
    try:
        for sub in video.parent.iterdir():
            if not sub.is_dir():
                continue
            folders.append((sub, sub.name, False))
            for sub2 in sub.iterdir():
                if sub2.is_dir():
                    folders.append((sub2, f"{sub.name} {sub2.name}", sub2.name.lower() == stem))
    except OSError:
        pass
    for folder, extra, any_name in folders:
        for f in files(folder):
            consider(f, extra, 0, any_name)
    return best[1] if best else None


def choose_sub_stream(info: MediaInfo, lang: str) -> Stream | None:
    """Полная (не «надписи») текстовая дорожка нужного языка."""
    def is_signs(s: Stream):
        return s.forced or any(w in s.title.lower() for w in SIGNS_WORDS)

    full = [s for s in info.subs if s.codec in TEXT_SUB_CODECS and not is_signs(s)]
    if not full:
        return None
    if not lang:
        return full[0]
    hits = [s for s in full if lang_matches(f"{s.lang} {s.title}", lang)]
    if hits:
        return max(hits, key=lambda s: s.default)
    undefined = [s for s in full if s.lang in ("", "und")]
    return undefined[0] if undefined else None


# ---------- распознавание речи ----------

def whisper_available() -> bool:
    return importlib.util.find_spec("faster_whisper") is not None


def _read_wav(path: str):
    """WAV 16 кГц моно (его готовит ffmpeg) -> массив float32 для Whisper."""
    import wave

    import numpy as np

    with wave.open(path, "rb") as w:
        data = w.readframes(w.getnframes())
    return np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0


def transcribe(wav: str, model_name: str, language: str, max_len: int, duration: float,
               runner, on_progress, log) -> list[Cue]:
    from faster_whisper import WhisperModel

    # Передаём уже прочитанный звук, а не путь: по пути faster-whisper открывает файл через
    # PyAV, а новые версии PyAV несовместимы с faster-whisper 1.2 (ошибка metadata_errors).
    audio = _read_wav(wav)
    lang = None if language in ("", "auto") else language
    device, compute = "cpu", "int8"
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:
            device, compute = "cuda", "float16"
    except Exception:
        pass

    def run(device, compute):
        model = WhisperModel(model_name, device=device, compute_type=compute,
                             cpu_threads=os.cpu_count() or 4)
        segments, _ = model.transcribe(audio, language=lang, vad_filter=True, word_timestamps=True,
                                       condition_on_previous_text=False)
        words = []
        for seg in segments:
            if runner.cancelled:
                raise Cancelled()
            words += [(w.start, w.end, w.word) for w in (seg.words or [])]
            on_progress(min(1.0, seg.end / duration))
        return words

    try:
        words = run(device, compute)
    except Cancelled:
        raise
    except Exception as e:
        if device == "cpu":
            raise
        log(f"  видеокарта для распознавания недоступна ({e}), продолжаю на процессоре")
        words = run("cpu", "int8")
    return words_to_cues(words, max_len)


def words_to_cues(words, max_len: int) -> list[Cue]:
    """Собирает слова в короткие фразы: по паузам, концу предложения и длине."""
    cues: list[Cue] = []
    cur: list[str] = []
    start = end = 0.0
    # Слова от Whisper уже несут пробел перед собой (« чуть», «-чуть»), поэтому склеиваем как есть
    for w_start, w_end, raw in words:
        if not raw.strip():
            continue
        if cur:
            joined = "".join(cur).strip()
            too_long = len(joined) + len(raw) > max_len or w_end - start > 5.0
            sentence_end = joined[-1] in ".!?…" and len(joined) >= max_len * 0.4
            if too_long or w_start - end > 0.7 or sentence_end:
                cues.append(Cue(start, end, joined))
                cur = []
        if not cur:
            start = w_start
        cur.append(raw)
        end = w_end
    if cur:
        cues.append(Cue(start, end, "".join(cur).strip()))
    for i, c in enumerate(cues):  # чуть дольше на экране, но без наложения на следующую
        nxt = cues[i + 1].start if i + 1 < len(cues) else c.end + 1
        c.end = max(c.end, min(c.end + 0.3, nxt))
    return cues


# ---------- раскладка по строкам ----------

def wrap(text: str, max_chars: int) -> list[str]:
    """Перенос по словам с выравниванием длины строк."""
    words = text.split()
    if not words:
        return []

    def greedy(width):
        lines, cur = [], ""
        for w in words:
            if not cur:
                cur = w
            elif len(cur) + 1 + len(w) <= width:
                cur += " " + w
            else:
                lines.append(cur)
                cur = w
        lines.append(cur)
        return lines

    base = greedy(max_chars)
    if len(base) == 1:
        return base
    total = len(" ".join(words))
    for width in range(math.ceil(total / len(base)), max_chars + 1):
        cand = greedy(width)
        if len(cand) <= len(base):
            return cand
    return base


def split_chunks(text: str, max_chars: int, max_lines: int) -> list[list[str]]:
    """Делит длинную фразу на экраны не больше max_lines строк, примерно поровну."""
    words = text.split()

    def greedy(budget):
        chunks, cur = [], []
        for w in words:
            trial = cur + [w]
            joined = " ".join(trial)
            if cur and (len(joined) > budget or len(wrap(joined, max_chars)) > max_lines):
                chunks.append(cur)
                cur = [w]
            else:
                cur = trial
        if cur:
            chunks.append(cur)
        return chunks

    chunks = greedy(len(text))
    if len(chunks) > 1:
        total = len(" ".join(words))
        for budget in range(math.ceil(total / len(chunks)), total + 1):
            balanced = greedy(budget)
            if len(balanced) <= len(chunks):
                chunks = balanced
                break
    return [wrap(" ".join(c), max_chars) for c in chunks]


def layout_cues(cues: list[Cue], style: SubtitleStyle) -> list[tuple[float, float, list[str]]]:
    out = []
    for c in cues:
        text = c.text.upper() if style.uppercase else c.text
        chunks = split_chunks(text, style.max_chars, style.max_lines)
        if not chunks:
            continue
        sizes = [len(" ".join(ch)) for ch in chunks]
        t, dur = c.start, c.end - c.start
        for ch, size in zip(chunks, sizes):
            d = dur * size / sum(sizes)
            out.append((t, t + d, ch))
            t += d
    return out


def lines_at(cues: list[Cue], style: SubtitleStyle, t: float) -> list[str] | None:
    """Строки, которые видны в момент t (для предпросмотра). None — в этот момент тишина."""
    for c in cues:
        if c.start <= t < c.end:
            for s, e, lines in layout_cues([c], style):
                if s <= t < e:
                    return lines
            return None
    return None


def sample_lines(style: SubtitleStyle) -> list[str]:
    text = "Так будут выглядеть субтитры в твоём ролике"
    if style.uppercase:
        text = text.upper()
    return split_chunks(text, style.max_chars, style.max_lines)[0]


def events_for_part(laid, start: float, end: float):
    out = []
    for s, e, lines in laid:
        if e <= start or s >= end:
            continue
        a, b = max(s, start) - start, min(e, end) - start
        if b - a >= 0.05:
            out.append((a, b, lines))
    return out


# ---------- запись ASS ----------

def ass_color(argb: str) -> str:
    """#AARRGGBB -> &HAABBGGRR (в ASS 00 — непрозрачный)."""
    h = argb.lstrip("#")
    if len(h) == 6:
        h = "FF" + h
    a, r, g, b = h[0:2], h[2:4], h[4:6], h[6:8]
    return f"&H{255 - int(a, 16):02X}{b}{g}{r}".upper()


def box_padding(style: SubtitleStyle) -> int:
    return max(4, round(style.size * 0.18))


def _ass_time(t: float) -> str:
    cs = max(0, int(round(t * 100)))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_escape(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")")


def write_ass(path: Path, events, style: SubtitleStyle, *, position_height: int = OUT_H) -> None:
    """events: [(начало, конец, строки)] относительно начала ролика."""
    font = style.font.replace(",", " ")
    if style.box:
        border, outline, shadow = 3, box_padding(style), 0
        outline_col = back_col = ass_color(style.box_color)
    else:
        border, outline, shadow = 1, style.outline, style.shadow
        outline_col, back_col = ass_color(style.outline_color), ass_color(style.shadow_color)
    y = round(position_height * style.position / 100)
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {OUT_W}\n"
        f"PlayResY: {OUT_H}\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Main,{font},{style.size},{ass_color(style.color)},&H000000FF,{outline_col},{back_col},"
        f"{-1 if style.bold else 0},0,0,0,100,100,0,0,{border},{outline:g},{shadow:g},5,40,40,0,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    rows = []
    for start, end, lines in events:
        text = "\\N".join(_ass_escape(line) for line in lines)
        rows.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Main,,0,0,0,,"
                    f"{{\\an5\\pos({OUT_W // 2},{y})}}{text}")
    path.write_text(header + "\n".join(rows) + "\n", encoding="utf-8")
