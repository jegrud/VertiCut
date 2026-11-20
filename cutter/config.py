"""Настройки программы и пути к служебным папкам."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = APP_DIR / "data"
CACHE_DIR = DATA_DIR / "cache"
WORK_DIR = DATA_DIR / "work"
FONTS_DIR = APP_DIR / "fonts"
SETTINGS_FILE = DATA_DIR / "settings.json"
QUEUE_FILE = DATA_DIR / "queue.json"

# Размер готового ролика для TikTok
OUT_W, OUT_H = 1080, 1920

# Какой кусок вырезается из центра исходного кадра: ключ -> (ширина, высота)
ASPECTS = {
    "4:3": (4, 3),
    "1:1": (1, 1),
    "4:5": (4, 5),
    "3:4": (3, 4),
    "9:16": (9, 16),
    "16:9": (16, 9),
}

VIDEO_EXTS = {
    ".mkv", ".mp4", ".avi", ".mov", ".m4v", ".ts", ".m2ts",
    ".webm", ".wmv", ".flv", ".mpg", ".mpeg",
}


@dataclass
class SubtitleStyle:
    font: str = "Arial"
    size: int = 70
    bold: bool = True
    uppercase: bool = False
    color: str = "#FFFFFFFF"          # цвета хранятся как #AARRGGBB
    outline_color: str = "#FF000000"
    outline: float = 5.0
    shadow_color: str = "#A0000000"
    shadow: float = 3.0
    box: bool = False
    box_color: str = "#B4000000"
    position: int = 50                # центр текста, % высоты экрана сверху
    max_chars: int = 22
    max_lines: int = 2


@dataclass
class Settings:
    style: SubtitleStyle = field(default_factory=SubtitleStyle)

    # Субтитры
    sub_source: str = "auto"          # auto | external | embedded | whisper | none
    sub_lang: str = "rus"
    whisper_model: str = "small"
    whisper_lang: str = "ru"

    # Видео
    aspect: str = "4:3"
    background: str = "blur"          # blur | black
    remove_bars: bool = True
    encoder: str = "libx264"
    quality_mode: str = "crf"         # crf | bitrate
    crf: int = 17
    bitrate: int = 18                 # Мбит/с
    max_bitrate: int = 20             # Мбит/с
    x264_preset: str = "medium"

    # Звук
    audio_lang: str = "rus"
    normalize: bool = True
    target_lufs: float = -14.0

    # Нарезка
    min_len: int = 75
    target_len: int = 80
    max_len: int = 90
    overlap: float = 1.5
    scene_threshold: int = 10
    tail_policy: str = "auto"         # auto | merge | separate | drop
    default_skip_start: str = "0:00"
    default_intro: str = ""
    default_skip_end: str = "0:00"

    # Результат
    series_name: str = ""
    output_dir: str = ""
    overwrite: bool = False
    bottom_video: str = ""            # пусто — обычная раскладка

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Settings":
        s = cls()
        _fill(s, data, skip={"style"})
        _fill(s.style, data.get("style") or {})
        return s


def _fill(obj, data: dict, skip=()) -> None:
    """Переносит значения из словаря, сохраняя типы и пропуская мусор."""
    for f in fields(obj):
        if f.name in skip or f.name not in data:
            continue
        current = getattr(obj, f.name)
        try:
            setattr(obj, f.name, type(current)(data[f.name]))
        except (TypeError, ValueError):
            pass


def ensure_dirs() -> None:
    for d in (DATA_DIR, CACHE_DIR, WORK_DIR, FONTS_DIR):
        d.mkdir(parents=True, exist_ok=True)


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_settings() -> Settings:
    data = read_json(SETTINGS_FILE, {})
    return Settings.from_dict(data if isinstance(data, dict) else {})


def save_settings(s: Settings) -> None:
    write_json(SETTINGS_FILE, s.to_dict())
