"""Главное окно: очередь серий, настройки и предпросмотр."""
from __future__ import annotations

import copy
import os
import re
import shutil
from pathlib import Path

from PySide6.QtCore import QObject, QRectF, QRunnable, QSize, Qt, QThread, QThreadPool, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QImage, QPainter, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QColorDialog, QComboBox, QDoubleSpinBox, QFileDialog,
    QFontComboBox, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSlider, QSpinBox,
    QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from . import subtitles as subs
from .config import APP_DIR, FONTS_DIR, OUT_H, VIDEO_EXTS, WORK_DIR, Settings, load_settings, save_settings
from .ffmpeg_tools import FFmpegError, MediaInfo, Runner, frame_seek_time, grab_frame, probe, tool_path
from .preview import compose
from .processor import (EpisodeCache, Job, Processor, get_bars, load_cues, load_queue, output_dir,
                        save_queue)
from .render import ENCODERS, bottom_video_filter, build_filter, detect_encoders, geometry, split_top_height
from .utils import fmt_time, guess_season_episode, natural_key, parse_interval, parse_time

ASPECTS = [
    ("4:3", "4:3 — по бокам срезается немного"),
    ("1:1", "1:1 — квадрат"),
    ("4:5", "4:5"),
    ("3:4", "3:4 — вертикальный 4:3"),
    ("9:16", "9:16 — на весь экран (сильная обрезка)"),
    ("16:9", "16:9 — без обрезки"),
]
SUB_SOURCES = [
    ("auto", "Авто: файл рядом с видео → встроенные → распознавание речи"),
    ("external", "Только файл рядом с видео (.srt / .ass)"),
    ("embedded", "Только встроенные в видео"),
    ("whisper", "Распознавание речи (Whisper)"),
    ("none", "Без субтитров"),
]
TAIL_POLICIES = [
    ("auto", "Авто: до 30 с — приклеить к последней части, длиннее — отдельной частью"),
    ("merge", "Всегда приклеивать к последней части"),
    ("separate", "Сохранять отдельной частью"),
    ("drop", "Отбрасывать"),
]
BACKGROUNDS = [("blur", "Размытая копия кадра"), ("black", "Чёрный")]
QUALITY = [("crf", "Постоянное качество (CRF)"), ("bitrate", "Фиксированный битрейт")]
X264_PRESETS = ["veryfast", "faster", "fast", "medium", "slow", "slower"]
WHISPER_MODELS = ["tiny", "base", "small", "medium", "large-v3"]

COL_FILE, COL_SEASON, COL_EP, COL_START, COL_INTRO, COL_END, COL_STATE = range(7)
EDITABLE_COLS = (COL_SEASON, COL_EP, COL_START, COL_INTRO, COL_END)
STATE_TEXT = {"pending": "ожидает", "running": "в работе", "done": "готово", "error": "ошибка"}
STATE_COLORS = {"done": "#43a047", "error": "#e53935", "running": "#1e88e5"}


# ---------- фоновые задачи для предпросмотра ----------

def _analyze(path):
    info = probe(path)
    return path, info, get_bars(info, EpisodeCache(path))


def _preview_cues(path, info, s):
    cues, src = load_cues(info, s, output_dir(s, path), EpisodeCache(path), Runner(),
                          lambda m: None, allow_whisper=False)
    return path, cues, src


def _grab(info, t, vf, req):
    data, _ = grab_frame(info, t, ["-map", f"0:{info.video.index}", "-vf", vf])
    return req, data


def _grab_bottom(info, t, height, req):
    vf = bottom_video_filter(info, height) + ",scale=540:-2"
    data, _ = grab_frame(info, t, ["-map", f"0:{info.video.index}", "-vf", vf], loop=True)
    return req, data


def _exact(info, s, bars, t, lines, req, bottom):
    ass_rel = None
    subtitle_height = split_top_height(s.aspect) if bottom else OUT_H
    if lines:
        subs.write_ass(WORK_DIR / "preview.ass", [(0.0, 36000.0, lines)], s.style,
                       position_height=subtitle_height)
        ass_rel = "data/work/preview.ass"
    extra_inputs = ()
    if bottom:
        bottom_t = frame_seek_time(bottom, t, loop=True)
        extra_inputs = ("-ss", f"{bottom_t:.3f}", "-an", "-sn", "-dn", "-i", bottom.path)
    vf = build_filter(info, s, bars, ass_rel, bottom=bottom)
    data, log = grab_frame(info, t, ["-filter_complex", vf, "-map", "[vout]"],
                           cwd=str(APP_DIR), loglevel="info", extra_inputs=extra_inputs)
    return req, data, re.findall(r"fontselect: \((.*?)\) -> ([^,\r\n]+)", log)


class _TaskSignals(QObject):
    done = Signal(object)
    failed = Signal(str)


class Task(QRunnable):
    def __init__(self, fn, *args):
        super().__init__()
        self.setAutoDelete(False)
        self.fn, self.args = fn, args
        self.signals = _TaskSignals()
        self.finished = False

    def run(self):
        try:
            result = self.fn(*self.args)
        except Exception as e:
            self.signals.failed.emit(str(e) or type(e).__name__)
        else:
            self.signals.done.emit(result)
        finally:
            self.finished = True


class Worker(QThread):
    log = Signal(str)
    progress = Signal(float, str)
    job_changed = Signal(object)

    def __init__(self, jobs: list[Job], settings: Settings):
        super().__init__()
        self.jobs = jobs
        self.processor = Processor(settings, self.log.emit, self.progress.emit, self.job_changed.emit)

    def run(self):
        try:
            self.processor.run(self.jobs)
        except Exception as e:
            self.log.emit(f"Критическая ошибка: {e}")

    def cancel(self):
        self.processor.cancel()


# ---------- виджеты ----------

class ColorButton(QPushButton):
    changed = Signal()

    def __init__(self, alpha=False):
        super().__init__()
        self._alpha = alpha
        self._color = "#FFFFFFFF"
        self.setFixedWidth(64)
        self.clicked.connect(self._pick)

    def color(self) -> str:
        return self._color

    def set_color(self, value: str) -> None:
        self._color = value
        c = QColor(value)
        self.setStyleSheet(f"QPushButton {{ background-color: {c.name()}; border: 1px solid #777;"
                           f" border-radius: 4px; min-height: 22px; }}")
        tip = c.name().upper()
        if self._alpha:
            tip += f", прозрачность {round((255 - c.alpha()) / 2.55)}%"
        self.setToolTip(tip)

    def _pick(self):
        if self._alpha:
            c = QColorDialog.getColor(QColor(self._color), self, "Выберите цвет",
                                      QColorDialog.ColorDialogOption.ShowAlphaChannel)
        else:
            c = QColorDialog.getColor(QColor(self._color), self, "Выберите цвет")
        if c.isValid():
            if not self._alpha:
                c.setAlpha(255)
            self.set_color(c.name(QColor.NameFormat.HexArgb))
            self.changed.emit()


class PreviewWidget(QWidget):
    def __init__(self):
        super().__init__()
        self._image: QImage | None = None
        self.setMinimumSize(270, 480)

    def sizeHint(self):
        return QSize(405, 720)

    def set_image(self, img: QImage) -> None:
        self._image = img
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#1e1e1e"))
        if self._image is None:
            return
        scale = min(self.width() / self._image.width(), self.height() / self._image.height())
        tw, th = self._image.width() * scale, self._image.height() * scale
        target = QRectF((self.width() - tw) / 2, (self.height() - th) / 2, tw, th)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.drawImage(target, self._image)


def _combo(items) -> QComboBox:
    cb = QComboBox()
    for key, label in items:
        cb.addItem(label, key)
    return cb


def _set_combo(cb: QComboBox, key) -> None:
    cb.setCurrentIndex(max(0, cb.findData(key)))


def _row(*widgets) -> QHBoxLayout:
    h = QHBoxLayout()
    for w in widgets:
        if isinstance(w, str):
            w = QLabel(w)
        h.addWidget(w)
    h.addStretch()
    return h


def _hint(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    lbl.setStyleSheet("color: #777;")
    return lbl


# ---------- главное окно ----------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Нарезка сериалов для TikTok")
        self.resize(1340, 900)
        self.settings = load_settings()
        self.jobs = load_queue()
        self.worker: Worker | None = None
        self.pool = QThreadPool.globalInstance()
        self._tasks: set[Task] = set()
        self._loading = False
        self._last_states: dict[int, str] = {}
        self.file_buttons: list[QPushButton] = []

        self.pv_path = None
        self.pv_info = None
        self.pv_bars = None
        self.pv_cues: list[subs.Cue] = []
        self.pv_frame: QImage | None = None
        self.pv_exact: QImage | None = None
        self.pv_req = 0
        self.pv_exact_req = 0
        self.pv_bottom_req = 0
        self.pv_bottom_info: MediaInfo | None = None
        self.pv_bottom_frame: QImage | None = None
        self.pv_bottom_error = ""

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(600)
        self._save_timer.timeout.connect(lambda: save_settings(self.settings))
        self._frame_timer = QTimer(self)
        self._frame_timer.setSingleShot(True)
        self._frame_timer.setInterval(250)
        self._frame_timer.timeout.connect(self._request_frame)

        self._build_ui()
        self._settings_to_ui()
        self._connect_settings()
        self._refresh_table()
        self._update_widgets_state()
        self._load_bottom_video()
        self._update_preview()
        QTimer.singleShot(0, self._startup_checks)

    # ----- построение интерфейса -----

    def _build_ui(self):
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_preview_panel())
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_files_tab(), "Серии")
        self.tabs.addTab(self._build_subs_tab(), "Субтитры")
        self.tabs.addTab(self._build_video_tab(), "Видео и звук")
        self.tabs.addTab(self._build_cut_tab(), "Нарезка")
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        self.tabs.addTab(self.log_view, "Журнал")
        rv.addWidget(self.tabs, 1)
        rv.addWidget(self._build_run_bar())
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([430, 910])
        self.setCentralWidget(splitter)

    def _build_preview_panel(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(QLabel("<b>Предпросмотр ролика</b>"))
        self.preview = PreviewWidget()
        v.addWidget(self.preview, 1)
        self.lbl_pv_file = QLabel("Серия не выбрана")
        self.lbl_pv_file.setWordWrap(True)
        v.addWidget(self.lbl_pv_file)
        self.sl_time = QSlider(Qt.Orientation.Horizontal)
        self.sl_time.setRange(0, 0)
        self.sl_time.valueChanged.connect(self._on_time_changed)
        self.lbl_time = QLabel("0:00 / 0:00")
        row = QHBoxLayout()
        row.addWidget(self.sl_time, 1)
        row.addWidget(self.lbl_time)
        v.addLayout(row)
        self.btn_exact = QPushButton("Показать точный кадр (через FFmpeg)")
        self.btn_exact.clicked.connect(self._exact_frame)
        v.addWidget(self.btn_exact)
        self.lbl_pv_mode = _hint("")
        v.addWidget(self.lbl_pv_mode)
        return w

    def _build_files_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        row = QHBoxLayout()
        for text, slot in (("Добавить серии…", self._add_files),
                           ("Добавить папку с сезоном…", self._add_folder),
                           ("Убрать выбранные", self._remove_selected),
                           ("Обработать заново", self._reset_selected),
                           ("Очистить список", self._clear_jobs)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            row.addWidget(b)
            self.file_buttons.append(b)
        row.addStretch()
        v.addLayout(row)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["Файл", "Сезон", "Серия", "Пропустить начало",
                                              "Вырезать заставку", "Пропустить конец", "Состояние"])
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(COL_FILE, QHeaderView.ResizeMode.Stretch)
        for c, width in ((COL_SEASON, 60), (COL_EP, 60), (COL_START, 125), (COL_INTRO, 125),
                         (COL_END, 125), (COL_STATE, 210)):
            hh.resizeSection(c, width)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._edit_triggers = self.table.editTriggers()
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        v.addWidget(self.table, 1)
        v.addWidget(_hint("Время пишите как 1:30. «Вырезать заставку» — интервал внутри серии, например "
                          "0:45-1:40 (пусто — ничего не вырезать). Значения меняются прямо в таблице, "
                          "двойным щелчком. Если номер серии не распознан из имени файла — поправьте его."))

        form = QFormLayout()
        self.le_series = QLineEdit()
        self.le_series.setPlaceholderText("Берётся из имени файла")
        form.addRow("Название сериала в именах файлов:", self.le_series)
        self.le_outdir = QLineEdit()
        self.le_outdir.setPlaceholderText("По умолчанию — папка TikTok рядом с серией")
        b_pick = QPushButton("Обзор…")
        b_pick.clicked.connect(self._pick_outdir)
        b_open = QPushButton("Открыть")
        b_open.clicked.connect(self._open_outdir)
        out_row = QHBoxLayout()
        out_row.addWidget(self.le_outdir, 1)
        out_row.addWidget(b_pick)
        out_row.addWidget(b_open)
        form.addRow("Папка для готовых роликов:", out_row)
        self.chk_overwrite = QCheckBox("Перезаписывать готовые части (иначе они пропускаются — "
                                       "так нарезка продолжается после сбоя)")
        form.addRow(self.chk_overwrite)
        v.addLayout(form)
        return w

    def _build_subs_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)

        src = QGroupBox("Откуда брать субтитры")
        f = QFormLayout(src)
        self.cb_sub_source = _combo(SUB_SOURCES)
        f.addRow("Источник:", self.cb_sub_source)
        self.le_sub_lang = QLineEdit()
        self.le_sub_lang.setFixedWidth(80)
        f.addRow("Язык субтитров:", _row(self.le_sub_lang, _hint("rus, eng, … — по нему выбирается "
                                                                 "встроенная дорожка и файл")))
        self.cb_whisper_model = QComboBox()
        self.cb_whisper_model.addItems(WHISPER_MODELS)
        self.cb_whisper_lang = QComboBox()
        self.cb_whisper_lang.setEditable(True)
        self.cb_whisper_lang.addItems(["ru", "en", "uk", "kk", "ky", "uz", "auto"])
        f.addRow("Распознавание:", _row("модель", self.cb_whisper_model, "язык речи", self.cb_whisper_lang))
        if subs.whisper_available():
            text = ("Установлено. Модели: tiny/base — быстро, small — баланс, medium/large-v3 — точнее, "
                    "но долго. Распознанный текст сохраняется в папку _subtitles — его можно поправить.")
        else:
            text = ("Распознавание речи не установлено. Чтобы включить, запустите install_whisper.bat "
                    "(понадобится интернет, около 1–3 ГБ).")
        f.addRow("", _hint(text))
        v.addWidget(src)

        st = QGroupBox("Как выглядят субтитры")
        f = QFormLayout(st)
        self.font_combo = QFontComboBox()
        b_font = QPushButton("Шрифт из файла…")
        b_font.clicked.connect(self._add_font_file)
        h = QHBoxLayout()
        h.addWidget(self.font_combo, 1)
        h.addWidget(b_font)
        f.addRow("Шрифт:", h)
        self.sp_size = QSpinBox()
        self.sp_size.setRange(16, 200)
        self.sp_size.setSuffix(" px")
        self.chk_bold = QCheckBox("Жирный")
        self.chk_upper = QCheckBox("ЗАГЛАВНЫЕ БУКВЫ")
        f.addRow("Размер:", _row(self.sp_size, self.chk_bold, self.chk_upper))
        self.btn_color = ColorButton()
        f.addRow("Цвет текста:", _row(self.btn_color))
        self.btn_outline_color = ColorButton()
        self.sp_outline = QDoubleSpinBox()
        self.sp_outline.setRange(0, 20)
        self.sp_outline.setSingleStep(0.5)
        f.addRow("Обводка:", _row(self.btn_outline_color, "толщина", self.sp_outline))
        self.btn_shadow_color = ColorButton(alpha=True)
        self.sp_shadow = QDoubleSpinBox()
        self.sp_shadow.setRange(0, 20)
        self.sp_shadow.setSingleStep(0.5)
        f.addRow("Тень:", _row(self.btn_shadow_color, "смещение", self.sp_shadow))
        self.chk_box = QCheckBox("Подложка под текстом")
        self.btn_box_color = ColorButton(alpha=True)
        f.addRow("", _row(self.chk_box, self.btn_box_color))
        self.sl_position = QSlider(Qt.Orientation.Horizontal)
        self.sl_position.setRange(5, 95)
        self.lbl_position = QLabel()
        h = QHBoxLayout()
        h.addWidget(self.sl_position, 1)
        h.addWidget(self.lbl_position)
        f.addRow("Положение по высоте:", h)
        self.sp_max_chars = QSpinBox()
        self.sp_max_chars.setRange(8, 60)
        self.sp_max_lines = QSpinBox()
        self.sp_max_lines.setRange(1, 4)
        f.addRow("Перенос:", _row("символов в строке", self.sp_max_chars, "строк на экране", self.sp_max_lines))
        f.addRow("", _hint("Длинная фраза делится на несколько экранов, время делится пропорционально тексту."))
        v.addWidget(st)
        v.addStretch()
        return w

    def _build_video_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)

        fr = QGroupBox("Кадр (ролик всегда 1080×1920)")
        f = QFormLayout(fr)
        self.cb_aspect = _combo(ASPECTS)
        f.addRow("Что вырезать из центра:", self.cb_aspect)
        self.cb_background = _combo(BACKGROUNDS)
        f.addRow("Фон сверху и снизу:", self.cb_background)
        self.chk_bars = QCheckBox("Убирать чёрные полосы, вшитые в видео")
        f.addRow("", self.chk_bars)
        self.le_bottom_video = QLineEdit()
        self.le_bottom_video.setReadOnly(True)
        self.le_bottom_video.setPlaceholderText("Не выбрано")
        b_pick = QPushButton("Выбрать видео…")
        b_pick.clicked.connect(self._pick_bottom_video)
        self.btn_clear_bottom = QPushButton("Убрать")
        self.btn_clear_bottom.clicked.connect(self.le_bottom_video.clear)
        bottom_row = QHBoxLayout()
        bottom_row.addWidget(self.le_bottom_video, 1)
        bottom_row.addWidget(b_pick)
        bottom_row.addWidget(self.btn_clear_bottom)
        f.addRow("Видео под сериалом:", bottom_row)
        f.addRow("", _hint("Серия будет сверху, выбранное видео — снизу. Короткое видео повторяется, "
                           "звук остаётся из серии."))
        v.addWidget(fr)

        enc = QGroupBox("Кодирование H.264")
        f = QFormLayout(enc)
        self.cb_encoder = QComboBox()
        f.addRow("Кодировщик:", self.cb_encoder)
        self.cb_quality = _combo(QUALITY)
        f.addRow("Режим:", self.cb_quality)
        self.sp_crf = QSpinBox()
        self.sp_crf.setRange(10, 30)
        f.addRow("Качество:", _row(self.sp_crf, _hint("меньше — лучше; 16–18 — почти без потерь")))
        self.sp_bitrate = QSpinBox()
        self.sp_bitrate.setRange(4, 60)
        self.sp_bitrate.setSuffix(" Мбит/с")
        self.sp_maxrate = QSpinBox()
        self.sp_maxrate.setRange(4, 80)
        self.sp_maxrate.setSuffix(" Мбит/с")
        f.addRow("Битрейт:", _row(self.sp_bitrate, "не выше", self.sp_maxrate))
        self.cb_preset = QComboBox()
        self.cb_preset.addItems(X264_PRESETS)
        f.addRow("Скорость x264:", _row(self.cb_preset, _hint("медленнее — чуть лучше сжатие")))
        f.addRow("", _hint("Выход: 1080×1920, H.264 High, yuv420p, частота кадров как в исходнике, "
                           "+faststart. TikTok всё равно пережимает видео, поэтому выше 20 Мбит/с смысла нет."))
        v.addWidget(enc)

        au = QGroupBox("Звук: AAC 320 кбит/с, 48 кГц, стерео")
        f = QFormLayout(au)
        self.le_audio_lang = QLineEdit()
        self.le_audio_lang.setFixedWidth(80)
        f.addRow("Язык дорожки:", _row(self.le_audio_lang, _hint("если в файле несколько озвучек")))
        self.chk_normalize = QCheckBox("Выравнивать громкость до")
        self.sp_lufs = QDoubleSpinBox()
        self.sp_lufs.setRange(-30, -6)
        self.sp_lufs.setSingleStep(0.5)
        self.sp_lufs.setSuffix(" LUFS")
        f.addRow("", _row(self.chk_normalize, self.sp_lufs))
        v.addWidget(au)
        v.addStretch()
        return w

    def _build_cut_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        g = QGroupBox("Длина частей")
        f = QFormLayout(g)
        self.sp_min, self.sp_target, self.sp_max = QSpinBox(), QSpinBox(), QSpinBox()
        for sp in (self.sp_min, self.sp_target, self.sp_max):
            sp.setRange(10, 600)
            sp.setSuffix(" с")
        f.addRow("Длина:", _row("от", self.sp_min, "желательно", self.sp_target, "до", self.sp_max))
        self.sp_overlap = QDoubleSpinBox()
        self.sp_overlap.setRange(0, 10)
        self.sp_overlap.setSingleStep(0.5)
        self.sp_overlap.setSuffix(" с")
        f.addRow("Повтор в начале следующей части:", _row(self.sp_overlap))
        self.sp_scene = QSpinBox()
        self.sp_scene.setRange(2, 60)
        f.addRow("Порог смены сцены:", _row(self.sp_scene, _hint("меньше — больше найденных склеек")))
        self.cb_tail = _combo(TAIL_POLICIES)
        f.addRow("Короткий остаток в конце:", self.cb_tail)
        f.addRow("", _hint("Разрез ставится там, где одновременно меняется сцена и никто не говорит. "
                           "Если такого места нет — в паузе между фразами, и только в крайнем случае — "
                           "посреди фразы."))
        v.addWidget(g)

        d = QGroupBox("Что пропускать (значения для новых серий в списке)")
        f = QFormLayout(d)
        self.le_def_start, self.le_def_intro, self.le_def_end = QLineEdit(), QLineEdit(), QLineEdit()
        for le in (self.le_def_start, self.le_def_intro, self.le_def_end):
            le.setFixedWidth(120)
        self.le_def_intro.setPlaceholderText("например 0:45-1:40")
        f.addRow("Пропустить начало:", _row(self.le_def_start))
        f.addRow("Вырезать заставку:", _row(self.le_def_intro))
        f.addRow("Пропустить конец (титры):", _row(self.le_def_end))
        b = QPushButton("Применить ко всем сериям в списке")
        b.clicked.connect(self._apply_defaults_to_all)
        f.addRow("", _row(b))
        v.addWidget(d)
        v.addStretch()
        return w

    def _build_run_bar(self):
        w = QWidget()
        h = QHBoxLayout(w)
        col = QVBoxLayout()
        self.lbl_status = QLabel("Готово к работе")
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        col.addWidget(self.lbl_status)
        col.addWidget(self.progress)
        h.addLayout(col, 1)
        self.btn_start = QPushButton("▶  Начать нарезку")
        self.btn_start.setMinimumHeight(40)
        self.btn_start.clicked.connect(self._start)
        self.btn_stop = QPushButton("■  Стоп")
        self.btn_stop.setMinimumHeight(40)
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop)
        h.addWidget(self.btn_start)
        h.addWidget(self.btn_stop)
        return w

    # ----- настройки <-> интерфейс -----

    def _settings_to_ui(self):
        self._loading = True
        s, st = self.settings, self.settings.style
        _set_combo(self.cb_sub_source, s.sub_source)
        self.le_sub_lang.setText(s.sub_lang)
        self.cb_whisper_model.setCurrentText(s.whisper_model)
        self.cb_whisper_lang.setCurrentText(s.whisper_lang)
        self.font_combo.setCurrentFont(QFont(st.font))
        self.sp_size.setValue(st.size)
        self.chk_bold.setChecked(st.bold)
        self.chk_upper.setChecked(st.uppercase)
        self.btn_color.set_color(st.color)
        self.btn_outline_color.set_color(st.outline_color)
        self.sp_outline.setValue(st.outline)
        self.btn_shadow_color.set_color(st.shadow_color)
        self.sp_shadow.setValue(st.shadow)
        self.chk_box.setChecked(st.box)
        self.btn_box_color.set_color(st.box_color)
        self.sl_position.setValue(st.position)
        self.sp_max_chars.setValue(st.max_chars)
        self.sp_max_lines.setValue(st.max_lines)

        _set_combo(self.cb_aspect, s.aspect)
        _set_combo(self.cb_background, s.background)
        self.chk_bars.setChecked(s.remove_bars)
        self.le_bottom_video.setText(s.bottom_video)
        self.cb_encoder.clear()
        self.cb_encoder.addItem(ENCODERS.get(s.encoder, s.encoder), s.encoder)
        _set_combo(self.cb_quality, s.quality_mode)
        self.sp_crf.setValue(s.crf)
        self.sp_bitrate.setValue(s.bitrate)
        self.sp_maxrate.setValue(s.max_bitrate)
        self.cb_preset.setCurrentText(s.x264_preset)
        self.le_audio_lang.setText(s.audio_lang)
        self.chk_normalize.setChecked(s.normalize)
        self.sp_lufs.setValue(s.target_lufs)

        self.sp_min.setValue(s.min_len)
        self.sp_target.setValue(s.target_len)
        self.sp_max.setValue(s.max_len)
        self.sp_overlap.setValue(s.overlap)
        self.sp_scene.setValue(s.scene_threshold)
        _set_combo(self.cb_tail, s.tail_policy)
        self.le_def_start.setText(s.default_skip_start)
        self.le_def_intro.setText(s.default_intro)
        self.le_def_end.setText(s.default_skip_end)

        self.le_series.setText(s.series_name)
        self.le_outdir.setText(s.output_dir)
        self.chk_overwrite.setChecked(s.overwrite)
        self._loading = False

    def _ui_to_settings(self):
        s, st = self.settings, self.settings.style
        s.sub_source = self.cb_sub_source.currentData()
        s.sub_lang = self.le_sub_lang.text().strip()
        s.whisper_model = self.cb_whisper_model.currentText()
        s.whisper_lang = self.cb_whisper_lang.currentText().strip()
        st.font = self.font_combo.currentFont().family() or st.font
        st.size = self.sp_size.value()
        st.bold = self.chk_bold.isChecked()
        st.uppercase = self.chk_upper.isChecked()
        st.color = self.btn_color.color()
        st.outline_color = self.btn_outline_color.color()
        st.outline = self.sp_outline.value()
        st.shadow_color = self.btn_shadow_color.color()
        st.shadow = self.sp_shadow.value()
        st.box = self.chk_box.isChecked()
        st.box_color = self.btn_box_color.color()
        st.position = self.sl_position.value()
        st.max_chars = self.sp_max_chars.value()
        st.max_lines = self.sp_max_lines.value()

        s.aspect = self.cb_aspect.currentData()
        s.background = self.cb_background.currentData()
        s.remove_bars = self.chk_bars.isChecked()
        s.bottom_video = self.le_bottom_video.text()
        s.encoder = self.cb_encoder.currentData() or "libx264"
        s.quality_mode = self.cb_quality.currentData()
        s.crf = self.sp_crf.value()
        s.bitrate = self.sp_bitrate.value()
        s.max_bitrate = self.sp_maxrate.value()
        s.x264_preset = self.cb_preset.currentText()
        s.audio_lang = self.le_audio_lang.text().strip()
        s.normalize = self.chk_normalize.isChecked()
        s.target_lufs = self.sp_lufs.value()

        s.min_len = self.sp_min.value()
        s.target_len = self.sp_target.value()
        s.max_len = self.sp_max.value()
        s.overlap = self.sp_overlap.value()
        s.scene_threshold = self.sp_scene.value()
        s.tail_policy = self.cb_tail.currentData()
        s.default_skip_start = self.le_def_start.text().strip()
        s.default_intro = self.le_def_intro.text().strip()
        s.default_skip_end = self.le_def_end.text().strip()

        s.series_name = self.le_series.text().strip()
        s.output_dir = self.le_outdir.text().strip()
        s.overwrite = self.chk_overwrite.isChecked()

    def _connect_settings(self):
        ch = self._on_setting_changed
        for cb in (self.cb_sub_source, self.cb_whisper_model, self.cb_aspect, self.cb_background,
                   self.cb_encoder, self.cb_quality, self.cb_preset, self.cb_tail):
            cb.currentIndexChanged.connect(ch)
        self.cb_whisper_lang.currentTextChanged.connect(ch)
        for le in (self.le_sub_lang, self.le_audio_lang, self.le_series, self.le_outdir, self.le_bottom_video,
                   self.le_def_start, self.le_def_intro, self.le_def_end):
            le.textChanged.connect(ch)
        for sp in (self.sp_size, self.sp_outline, self.sp_shadow, self.sp_max_chars, self.sp_max_lines,
                   self.sp_crf, self.sp_bitrate, self.sp_maxrate, self.sp_lufs, self.sp_min,
                   self.sp_target, self.sp_max, self.sp_overlap, self.sp_scene):
            sp.valueChanged.connect(ch)
        for c in (self.chk_bold, self.chk_upper, self.chk_box, self.chk_bars, self.chk_normalize,
                  self.chk_overwrite):
            c.toggled.connect(ch)
        for b in (self.btn_color, self.btn_outline_color, self.btn_shadow_color, self.btn_box_color):
            b.changed.connect(ch)
        self.font_combo.currentFontChanged.connect(ch)
        self.sl_position.valueChanged.connect(ch)

    def _on_setting_changed(self, *_):
        if self._loading:
            return
        before = self.settings.to_dict()
        old = (self.settings.remove_bars, self.settings.sub_source, self.settings.sub_lang,
               self.settings.output_dir)
        old_bottom, old_aspect = self.settings.bottom_video, self.settings.aspect
        self._ui_to_settings()
        if self.settings.to_dict() == before:  # виджет сообщил об изменении, но значения те же
            return
        self._save_timer.start()
        self._update_widgets_state()
        self.pv_exact = None
        self.pv_exact_req += 1
        if self.settings.bottom_video != old_bottom:
            self._load_bottom_video()
        elif self.settings.bottom_video and self.settings.aspect != old_aspect:
            self._request_bottom_frame()
        if self.settings.remove_bars != old[0]:
            self._frame_timer.start()
        if (self.settings.sub_source, self.settings.sub_lang, self.settings.output_dir) != old[1:]:
            self._reload_preview_cues()
        self._update_preview()

    def _update_widgets_state(self):
        s = self.settings
        self.le_bottom_video.setToolTip(s.bottom_video)
        self.btn_clear_bottom.setEnabled(bool(s.bottom_video))
        self.cb_background.setEnabled(not s.bottom_video)
        crf = s.quality_mode == "crf"
        self.sp_crf.setEnabled(crf)
        self.sp_bitrate.setEnabled(not crf)
        self.cb_preset.setEnabled(s.encoder == "libx264")
        self.btn_box_color.setEnabled(s.style.box)
        for wdg in (self.btn_outline_color, self.sp_outline, self.btn_shadow_color, self.sp_shadow):
            wdg.setEnabled(not s.style.box)
        self.sp_lufs.setEnabled(s.normalize)
        position_ref = "кадра серии" if s.bottom_video else "ролика"
        self.lbl_position.setText(f"{s.style.position}% от верха {position_ref}")
        whisper = s.sub_source in ("auto", "whisper")
        self.cb_whisper_model.setEnabled(whisper)
        self.cb_whisper_lang.setEnabled(whisper)

    # ----- фоновые задачи -----

    def _run(self, fn, *args, on_done=None, on_fail=None):
        self._tasks = {t for t in self._tasks if not t.finished}
        task = Task(fn, *args)
        if on_done:
            task.signals.done.connect(on_done)
        task.signals.failed.connect(on_fail or (lambda msg: self._log(f"! {msg}")))
        self._tasks.add(task)
        self.pool.start(task)

    def _startup_checks(self):
        try:
            tool_path("ffmpeg")
            tool_path("ffprobe")
        except FFmpegError as e:
            self.btn_start.setEnabled(False)
            QMessageBox.critical(self, "Нужен FFmpeg", str(e))
            return
        self.lbl_status.setText("Проверяю кодировщики…")
        self._run(detect_encoders, on_done=self._on_encoders)
        if self.jobs:
            self.table.selectRow(0)

    def _on_encoders(self, found):
        found = found or ["libx264"]
        self._loading = True
        self.cb_encoder.clear()
        for enc in found:
            self.cb_encoder.addItem(ENCODERS[enc], enc)
        idx = self.cb_encoder.findData(self.settings.encoder)
        self.cb_encoder.setCurrentIndex(idx if idx >= 0 else 0)
        self._loading = False
        self._ui_to_settings()
        self._update_widgets_state()
        self.lbl_status.setText("Готово к работе")
        self._log("Доступные кодировщики: " + ", ".join(ENCODERS[e].split(" — ")[0] for e in found))

    def _log(self, text: str):
        self.log_view.appendPlainText(text)

    # ----- список серий -----

    def _state_text(self, job: Job) -> str:
        return job.message or STATE_TEXT.get(job.state, job.state)

    def _state_color(self, state: str) -> QColor:
        return QColor(STATE_COLORS[state]) if state in STATE_COLORS else self.palette().color(QPalette.ColorRole.Text)

    def _refresh_table(self):
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.jobs))
        for r, job in enumerate(self.jobs):
            values = [job.name, str(job.season), str(job.episode), job.skip_start, job.intro,
                      job.skip_end, self._state_text(job)]
            for c, val in enumerate(values):
                item = self.table.item(r, c)
                if item is None:
                    item = QTableWidgetItem()
                    self.table.setItem(r, c, item)
                item.setText(val)
                flags = Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
                if c in EDITABLE_COLS:
                    flags |= Qt.ItemFlag.ItemIsEditable
                item.setFlags(flags)
            self.table.item(r, COL_FILE).setToolTip(job.path)
            self.table.item(r, COL_STATE).setForeground(self._state_color(job.state))
            self._validate_row(r)
        self.table.blockSignals(False)

    def _validate_row(self, r: int):
        job = self.jobs[r]
        for c, fn, val in ((COL_START, parse_time, job.skip_start), (COL_INTRO, parse_interval, job.intro),
                           (COL_END, parse_time, job.skip_end)):
            item = self.table.item(r, c)
            try:
                fn(val)
                item.setData(Qt.ItemDataRole.BackgroundRole, None)
                item.setToolTip("")
            except ValueError as e:
                item.setBackground(QColor("#ffcdd2"))
                item.setToolTip(str(e))

    def _on_item_changed(self, item: QTableWidgetItem):
        r, c = item.row(), item.column()
        if r >= len(self.jobs):
            return
        job = self.jobs[r]
        text = item.text().strip()
        self.table.blockSignals(True)
        if c in (COL_SEASON, COL_EP):
            current = job.season if c == COL_SEASON else job.episode
            try:
                val = max(0, int(text))
            except ValueError:
                val = current
            if c == COL_SEASON:
                job.season = val
            else:
                job.episode = val
            item.setText(str(val))
        elif c == COL_START:
            job.skip_start = text
        elif c == COL_INTRO:
            job.intro = text
        elif c == COL_END:
            job.skip_end = text
        self._validate_row(r)
        self.table.blockSignals(False)
        save_queue(self.jobs)

    def _selected_rows(self) -> list[int]:
        return sorted({i.row() for i in self.table.selectedIndexes()})

    def _last_dir(self) -> str:
        return str(Path(self.jobs[-1].path).parent) if self.jobs else str(Path.home())

    def _add_files(self):
        exts = " ".join("*" + e for e in sorted(VIDEO_EXTS))
        files, _ = QFileDialog.getOpenFileNames(self, "Выберите серии", self._last_dir(),
                                                f"Видео ({exts});;Все файлы (*)")
        if files:
            self._add_paths(files)

    def _add_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Папка с сезоном", self._last_dir())
        if not d:
            return
        root = Path(d)
        files = [f for f in root.iterdir() if f.is_file() and f.suffix.lower() in VIDEO_EXTS]
        if not files:
            files = [f for f in root.rglob("*") if f.is_file() and f.suffix.lower() in VIDEO_EXTS
                     and "tiktok" not in (p.lower() for p in f.parts)]
        files = [f for f in files if "sample" not in f.name.lower()]
        if not files:
            QMessageBox.information(self, "Нет видео", "В этой папке не найдено видеофайлов.")
            return
        self._add_paths([str(f) for f in files])

    def _add_paths(self, paths):
        existing = {os.path.normcase(os.path.abspath(j.path)) for j in self.jobs}
        next_ep = max((j.episode for j in self.jobs), default=0) + 1
        first_new = None
        for p in sorted(paths, key=lambda x: natural_key(Path(x).name)):
            full = os.path.abspath(p)
            if os.path.normcase(full) in existing:
                continue
            season, ep = guess_season_episode(Path(full).stem)
            job = Job(path=full, season=season or 1, episode=ep or next_ep,
                      skip_start=self.settings.default_skip_start, intro=self.settings.default_intro,
                      skip_end=self.settings.default_skip_end)
            next_ep = max(next_ep, job.episode + 1)
            self.jobs.append(job)
            existing.add(os.path.normcase(full))
            if first_new is None:
                first_new = len(self.jobs) - 1
        self._refresh_table()
        save_queue(self.jobs)
        if first_new is not None and self.pv_path is None:
            self.table.selectRow(first_new)

    def _remove_selected(self):
        for r in reversed(self._selected_rows()):
            if r < len(self.jobs):
                if self.jobs[r].path == self.pv_path:
                    self.pv_path = None
                del self.jobs[r]
        self._refresh_table()
        save_queue(self.jobs)

    def _reset_selected(self):
        rows = self._selected_rows() or range(len(self.jobs))
        for r in rows:
            self.jobs[r].state, self.jobs[r].message = "pending", ""
        self._refresh_table()
        save_queue(self.jobs)

    def _clear_jobs(self):
        if not self.jobs:
            return
        if QMessageBox.question(self, "Очистить список", "Убрать все серии из списка? "
                                "Готовые ролики останутся на диске.") != QMessageBox.StandardButton.Yes:
            return
        self.jobs.clear()
        self.pv_path = self.pv_info = None
        self.pv_frame = None
        self.pv_cues = []
        self.sl_time.setRange(0, 0)
        self.lbl_pv_file.setText("Серия не выбрана")
        self._refresh_table()
        self._update_preview()
        save_queue(self.jobs)

    def _apply_defaults_to_all(self):
        self._ui_to_settings()
        for job in self.jobs:
            job.skip_start = self.settings.default_skip_start
            job.intro = self.settings.default_intro
            job.skip_end = self.settings.default_skip_end
        self._refresh_table()
        save_queue(self.jobs)

    def _pick_outdir(self):
        d = QFileDialog.getExistingDirectory(self, "Папка для готовых роликов",
                                             self.le_outdir.text() or self._last_dir())
        if d:
            self.le_outdir.setText(d)

    def _pick_bottom_video(self):
        exts = " ".join("*" + e for e in sorted(VIDEO_EXTS))
        path, _ = QFileDialog.getOpenFileName(self, "Выберите видео под сериалом",
                                              self.le_bottom_video.text() or self._last_dir(),
                                              f"Видео ({exts});;Все файлы (*)")
        if path:
            self.le_bottom_video.setText(path)

    def _open_outdir(self):
        if self.settings.output_dir:
            path = Path(self.settings.output_dir)
        elif self.jobs:
            rows = self._selected_rows()
            path = output_dir(self.settings, self.jobs[rows[0] if rows else 0].path)
        else:
            return
        if path.is_dir():
            os.startfile(path)
        else:
            QMessageBox.information(self, "Папки ещё нет", f"Папка появится после нарезки:\n{path}")

    # ----- запуск -----

    def _start(self):
        if self.worker:
            return
        self._ui_to_settings()
        save_settings(self.settings)
        s = self.settings
        if s.bottom_video and not Path(s.bottom_video).is_file():
            QMessageBox.warning(self, "Видео под сериалом", f"Файл не найден:\n{s.bottom_video}")
            self.tabs.setCurrentIndex(2)
            return
        if not (s.min_len <= s.target_len <= s.max_len):
            QMessageBox.warning(self, "Длина частей", "Должно быть: «от» ≤ «желательно» ≤ «до».")
            self.tabs.setCurrentIndex(3)
            return
        todo = [j for j in self.jobs if j.state != "done"]
        if not todo:
            QMessageBox.information(self, "Нечего делать", "Нет серий для обработки. Добавьте серии "
                                    "или выделите готовые и нажмите «Обработать заново».")
            return
        problems = []
        for j in todo:
            try:
                parse_time(j.skip_start)
                parse_interval(j.intro)
                parse_time(j.skip_end)
            except ValueError as e:
                problems.append(f"{j.name}: {e}")
        if problems:
            QMessageBox.warning(self, "Исправьте время в таблице", "\n".join(problems[:10]))
            return
        self._log(f"=== Старт: серий в очереди — {len(todo)} ===")
        self._run_jobs = todo
        self.worker = Worker(self.jobs, copy.deepcopy(s))
        self.worker.log.connect(self._log)
        self.worker.progress.connect(self._on_progress)
        self.worker.job_changed.connect(self._on_job_changed)
        self.worker.finished.connect(self._on_finished)
        self._set_running(True)
        self.progress.setValue(0)
        self.worker.start()

    def _stop(self):
        if self.worker:
            self.worker.cancel()
            self.btn_stop.setEnabled(False)
            self.lbl_status.setText("Останавливаю…")

    def _set_running(self, on: bool):
        self.btn_start.setEnabled(not on)
        self.btn_stop.setEnabled(on)
        for b in self.file_buttons:
            b.setEnabled(not on)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers if on else self._edit_triggers)

    def _on_progress(self, frac: float, text: str):
        self.progress.setValue(int(frac * 1000))
        self.lbl_status.setText(text)

    def _on_job_changed(self, job: Job):
        r = next((i for i, j in enumerate(self.jobs) if j is job), None)
        if r is None:
            return
        item = self.table.item(r, COL_STATE)
        self.table.blockSignals(True)
        item.setText(self._state_text(job))
        item.setForeground(self._state_color(job.state))
        self.table.blockSignals(False)
        if self._last_states.get(id(job)) != job.state:
            self._last_states[id(job)] = job.state
            save_queue(self.jobs)

    def _on_finished(self):
        cancelled = self.worker.processor.runner.cancelled
        self.worker = None
        self._set_running(False)
        self._refresh_table()
        save_queue(self.jobs)
        done = sum(j.state == "done" for j in self._run_jobs)
        errors = sum(j.state == "error" for j in self._run_jobs)
        if cancelled:
            msg = "Остановлено. Готовые части сохранены — следующий запуск продолжит с места остановки."
        else:
            msg = f"Готово. Серий нарезано: {done}" + (f", с ошибками: {errors} (см. «Журнал»)" if errors else "")
            self.progress.setValue(1000)
        self.lbl_status.setText(msg)
        self._log(f"=== {msg} ===")

    def closeEvent(self, e):
        if self.worker:
            answer = QMessageBox.question(self, "Идёт нарезка", "Остановить нарезку и закрыть программу?\n"
                                          "Готовые части сохранятся, в следующий раз нарезка продолжится.")
            if answer != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
            self.worker.cancel()
            self.worker.wait(15000)
        self._ui_to_settings()
        save_settings(self.settings)
        save_queue(self.jobs)
        e.accept()

    # ----- предпросмотр -----

    def _on_selection_changed(self):
        rows = self._selected_rows()
        if rows and rows[0] < len(self.jobs) and self.jobs[rows[0]].path != self.pv_path:
            self._load_preview(self.jobs[rows[0]].path)

    def _load_preview(self, path: str):
        self.pv_req += 1
        self.pv_exact_req += 1
        self.pv_path, self.pv_info, self.pv_bars = path, None, None
        self.pv_cues, self.pv_frame, self.pv_exact = [], None, None
        self.lbl_pv_file.setText(f"{Path(path).name} — анализ…")
        self._update_preview()
        self._run(_analyze, path, on_done=self._on_analyzed,
                  on_fail=lambda m: self.lbl_pv_file.setText(f"Не удалось открыть: {m}"))

    def _on_analyzed(self, res):
        path, info, bars = res
        if path != self.pv_path:
            return
        self.pv_info, self.pv_bars = info, bars
        self.sl_time.blockSignals(True)
        self.sl_time.setRange(0, max(0, int(info.duration) - 1))
        self.sl_time.setValue(int(info.duration * 0.3))
        self.sl_time.blockSignals(False)
        self._update_time_label()
        self.lbl_pv_file.setText(f"{Path(path).name}\n{info.width}×{info.height}, {float(info.fps):.3f} к/с, "
                                 f"{fmt_time(info.duration)}, чёрные полосы: {'есть' if bars else 'нет'}")
        self._request_frame()
        self._reload_preview_cues()

    def _reload_preview_cues(self):
        if self.pv_info is None:
            return
        self._run(_preview_cues, self.pv_path, self.pv_info, copy.deepcopy(self.settings),
                  on_done=self._on_preview_cues, on_fail=lambda m: None)

    def _on_preview_cues(self, res):
        path, cues, _ = res
        if path == self.pv_path:
            self.pv_cues = cues
            self._update_preview()

    def _update_time_label(self):
        total = self.pv_info.duration if self.pv_info else 0
        self.lbl_time.setText(f"{fmt_time(self.sl_time.value())} / {fmt_time(total)}")

    def _on_time_changed(self):
        self._update_time_label()
        self.pv_exact = None
        self.pv_exact_req += 1
        self._update_preview()
        self._frame_timer.start()

    def _request_frame(self):
        self._request_bottom_frame()
        if self.pv_info is None:
            return
        self.pv_req += 1
        g = geometry(self.pv_info, self.pv_bars if self.settings.remove_bars else None, self.settings.aspect)
        vf = ",".join(g.pre + ["scale='min(1280,iw)':-2"])
        self._run(_grab, self.pv_info, float(self.sl_time.value()), vf, self.pv_req, on_done=self._on_frame,
                  on_fail=lambda m: self.lbl_pv_mode.setText(f"Кадр не получен: {m[:300]}"))

    def _on_frame(self, res):
        req, data = res
        if req != self.pv_req:
            return
        img = QImage.fromData(data, "PNG")
        if not img.isNull():
            self.pv_frame = img
        self._update_preview()

    def _load_bottom_video(self):
        self.pv_bottom_req += 1
        self.pv_bottom_info = self.pv_bottom_frame = None
        self.pv_bottom_error = ""
        path = self.settings.bottom_video
        if not path:
            return
        req = self.pv_bottom_req

        def loaded(info):
            if req == self.pv_bottom_req:
                self.pv_bottom_info = info
                self._request_bottom_frame()

        self._run(probe, path, on_done=loaded,
                  on_fail=lambda m: self._on_bottom_fail(req, m))

    def _request_bottom_frame(self):
        if self.pv_bottom_info is None:
            return
        self.pv_bottom_req += 1
        req = self.pv_bottom_req
        height = OUT_H - split_top_height(self.settings.aspect)
        self._run(_grab_bottom, self.pv_bottom_info, float(self.sl_time.value()), height, req,
                  on_done=self._on_bottom_frame, on_fail=lambda m: self._on_bottom_fail(req, m))

    def _on_bottom_frame(self, res):
        req, data = res
        if req != self.pv_bottom_req:
            return
        self.pv_bottom_frame = QImage.fromData(data, "PNG")
        self.pv_bottom_error = ""
        self._update_preview()

    def _on_bottom_fail(self, req, msg):
        if req == self.pv_bottom_req:
            self.pv_bottom_error = f"Видео под сериалом: {msg[:250]}"
            self._update_preview()

    def _preview_lines(self) -> tuple[list[str], str]:
        st = self.settings.style
        if self.settings.sub_source == "none":
            return [], "субтитры отключены"
        if self.pv_cues:
            lines = subs.lines_at(self.pv_cues, st, float(self.sl_time.value()))
            if lines:
                return lines, "текст из субтитров серии"
            return subs.sample_lines(st), "в этот момент никто не говорит, показан пример текста"
        return subs.sample_lines(st), "показан пример текста"

    def _update_preview(self):
        if self.pv_exact is not None:
            self.preview.set_image(self.pv_exact)
            return
        lines, note = self._preview_lines()
        self.preview.set_image(compose(self.pv_frame, self.settings, lines, self.pv_bottom_frame))
        status = f"Быстрый предпросмотр, примерно как в ролике: {note}."
        if self.settings.bottom_video and self.pv_bottom_frame is None:
            status += " Загружаю видео под сериалом…"
        self.lbl_pv_mode.setText(self.pv_bottom_error or status)

    def _exact_frame(self):
        if self.pv_info is None:
            QMessageBox.information(self, "Предпросмотр", "Сначала добавьте серию и выберите её в списке.")
            return
        if self.settings.bottom_video and self.pv_bottom_info is None:
            self.lbl_pv_mode.setText(self.pv_bottom_error or "Загружаю видео под сериалом…")
            return
        lines, _ = self._preview_lines()
        self.pv_exact_req += 1
        req = self.pv_exact_req
        self.btn_exact.setEnabled(False)
        self.lbl_pv_mode.setText("FFmpeg рисует кадр…")
        self._run(_exact, self.pv_info, copy.deepcopy(self.settings), self.pv_bars,
                  float(self.sl_time.value()), lines, req, self.pv_bottom_info,
                  on_done=self._on_exact, on_fail=lambda m: self._on_exact_fail(req, m))

    def _on_exact(self, res):
        self.btn_exact.setEnabled(True)
        req, data, fonts = res
        if req != self.pv_exact_req:
            return
        img = QImage.fromData(data, "PNG")
        if img.isNull():
            return
        self.pv_exact = img
        self.preview.set_image(img)
        note = ("Точный предпросмотр раскладки и субтитров." if self.settings.bottom_video
                else "Точный кадр: именно так он будет выглядеть в ролике.")
        if fonts:
            used = fonts[-1][1].strip()
            wanted = self.settings.style.font.replace(" ", "").lower()
            if wanted in used.replace(" ", "").lower():
                note += f" Шрифт: {used}."
            else:
                note += (f" Похоже, FFmpeg взял другой шрифт ({used}). Если надпись отличается от быстрого "
                         f"предпросмотра — добавьте файл шрифта кнопкой «Шрифт из файла…».")
        self.lbl_pv_mode.setText(note)

    def _on_exact_fail(self, req, msg: str):
        self.btn_exact.setEnabled(True)
        if req == self.pv_exact_req:
            self.lbl_pv_mode.setText(f"Не удалось получить кадр: {msg[:300]}")

    def _add_font_file(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Файл шрифта", "", "Шрифты (*.ttf *.otf *.ttc)")
        family = None
        for f in files:
            dst = FONTS_DIR / Path(f).name
            try:
                if not dst.exists():
                    shutil.copy2(f, dst)
            except OSError as e:
                QMessageBox.warning(self, "Шрифт", f"Не удалось скопировать шрифт: {e}")
                continue
            families = QFontDatabase.applicationFontFamilies(QFontDatabase.addApplicationFont(str(dst)))
            if families:
                family = families[0]
        if family:
            self.font_combo.setCurrentFont(QFont(family))
