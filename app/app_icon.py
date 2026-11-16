"""Load the unchanged user-provided Kontakt icon for Qt windows and dialogs."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QIcon


def application_icon_path() -> Path:
    """Absolute path works in editable installs and in PyInstaller onedir."""
    return Path(__file__).resolve().parent / "assets" / "kontakt-icon.ico"


def load_application_icon() -> QIcon:
    path = application_icon_path()
    if not path.is_file():
        raise FileNotFoundError(f"Отсутствует иконка приложения: {path}")
    icon = QIcon(str(path))
    if icon.isNull() or icon.pixmap(32, 32).isNull():
        raise RuntimeError(f"Qt не может прочитать иконку приложения: {path}")
    return icon
