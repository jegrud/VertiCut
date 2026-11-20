"""Быстрый предпросмотр кадра средствами Qt (без FFmpeg), повторяет раскладку из render.py."""
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import (QColor, QFont, QFontMetricsF, QImage, QLinearGradient, QPainter,
                           QPainterPath, QPainterPathStroker)

from .config import ASPECTS, OUT_H, OUT_W, Settings, SubtitleStyle
from .render import split_top_height
from .subtitles import box_padding


def placeholder_frame() -> QImage:
    img = QImage(1280, 720, QImage.Format.Format_RGB32)
    p = QPainter(img)
    grad = QLinearGradient(0, 0, 1280, 720)
    grad.setColorAt(0, QColor("#2f3e5c"))
    grad.setColorAt(1, QColor("#7d4e57"))
    p.fillRect(img.rect(), grad)
    p.setPen(QColor(255, 255, 255, 140))
    f = QFont()
    f.setPixelSize(40)
    p.setFont(f)
    p.drawText(img.rect(), Qt.AlignmentFlag.AlignCenter, "Добавьте серию, чтобы увидеть её кадр")
    p.end()
    return img


def compose(frame: QImage | None, s: Settings, lines: list[str],
            bottom_frame: QImage | None = None) -> QImage:
    """Собирает кадр 1080×1920: картинка из центра, фон, субтитры."""
    if frame is None or frame.isNull():
        frame = placeholder_frame()
    out = QImage(OUT_W, OUT_H, QImage.Format.Format_RGB32)
    out.fill(QColor("black"))
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)

    fw, fh = frame.width(), frame.height()
    aw, ah = ASPECTS.get(s.aspect, (4, 3))
    r = aw / ah
    fg_h = min(OUT_H, OUT_W / r)
    split = bool(s.bottom_video)
    if split:
        fg_h = split_top_height(s.aspect)
        r = OUT_W / fg_h
    cw, ch = (fh * r, fh) if fw / fh > r else (fw, fw / r)
    src = QRectF((fw - cw) / 2, (fh - ch) / 2, cw, ch)
    dst = QRectF(0, 0 if split else (OUT_H - fg_h) / 2, OUT_W, fg_h)

    if split:
        if bottom_frame is not None and not bottom_frame.isNull():
            p.drawImage(QRectF(0, fg_h, OUT_W, OUT_H - fg_h), bottom_frame)
    elif fg_h < OUT_H and s.background == "blur":
        small = frame.scaled(27, 48, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                             Qt.TransformationMode.SmoothTransformation)
        small = small.copy((small.width() - 27) // 2, (small.height() - 48) // 2, 27, 48)
        p.drawImage(out.rect(), small.scaled(OUT_W, OUT_H, Qt.AspectRatioMode.IgnoreAspectRatio,
                                             Qt.TransformationMode.SmoothTransformation))
        p.fillRect(out.rect(), QColor(0, 0, 0, 40))
    p.drawImage(dst, frame, src)
    draw_subtitles(p, s.style, lines, position_height=fg_h if split else OUT_H)
    p.end()
    return out


def _font(style: SubtitleStyle, device) -> QFont:
    # libass задаёт размер по полной высоте шрифта (подъём + спуск), Qt — по кеглю: пересчитываем
    font = QFont(style.font)
    font.setBold(style.bold)
    font.setPixelSize(max(1, style.size))
    fm = QFontMetricsF(font, device)
    real = fm.ascent() + fm.descent()
    if real > 0:
        font.setPixelSize(max(1, round(style.size * style.size / real)))
    return font


def _grown(path: QPainterPath, width: float) -> QPainterPath:
    if width <= 0:
        return path
    st = QPainterPathStroker()
    st.setWidth(width * 2)
    st.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    st.setCapStyle(Qt.PenCapStyle.RoundCap)
    return st.createStroke(path).united(path)


def draw_subtitles(p: QPainter, style: SubtitleStyle, lines: list[str],
                   *, position_height: float = OUT_H) -> None:
    if not lines:
        return
    font = _font(style, p.device())
    fm = QFontMetricsF(font, p.device())
    line_h = fm.ascent() + fm.descent()
    top = position_height * style.position / 100 - line_h * len(lines) / 2
    pad = box_padding(style)
    for i, text in enumerate(lines):
        w = fm.horizontalAdvance(text)
        x = OUT_W / 2 - w / 2
        y = top + i * line_h
        path = QPainterPath()
        path.addText(x, y + fm.ascent(), font, text)
        if style.box:
            p.fillRect(QRectF(x - pad, y - pad, w + 2 * pad, line_h + 2 * pad), QColor(style.box_color))
        else:
            outlined = _grown(path, style.outline)
            if style.shadow > 0:
                p.fillPath(outlined.translated(style.shadow, style.shadow), QColor(style.shadow_color))
            if style.outline > 0:
                p.fillPath(outlined, QColor(style.outline_color))
        p.fillPath(path, QColor(style.color))
