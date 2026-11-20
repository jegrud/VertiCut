"""Нарезка сериалов на вертикальные ролики для TikTok."""
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtGui import QFontDatabase  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from cutter.config import DATA_DIR, FONTS_DIR, ensure_dirs  # noqa: E402


def _excepthook(exc_type, exc, tb):
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    try:
        with open(DATA_DIR / "error.log", "a", encoding="utf-8") as f:
            f.write(text + "\n")
    except OSError:
        pass
    QMessageBox.critical(None, "Ошибка", text[-2000:])


def main():
    ensure_dirs()
    app = QApplication(sys.argv)
    app.setApplicationName("Нарезка для TikTok")
    app.setStyle("Fusion")
    sys.excepthook = _excepthook
    for font in list(FONTS_DIR.glob("*.ttf")) + list(FONTS_DIR.glob("*.otf")) + list(FONTS_DIR.glob("*.ttc")):
        QFontDatabase.addApplicationFont(str(font))

    from cutter.gui import MainWindow

    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
