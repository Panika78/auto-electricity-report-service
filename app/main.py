"""Запуск настольного приложения: python -m app.main."""

import sqlite3
import sys
from pathlib import Path

from app.data.database import Database
from app.version import APP_VERSION


def main(db_path: Path | str | None = None) -> int:
    # Импорт GUI при запуске, чтобы слой данных можно было тестировать без Qt.
    from PySide6.QtWidgets import QApplication, QMessageBox
    from app.app_icon import load_application_icon
    from app.ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    app.setApplicationName("контакт")
    app.setApplicationVersion(APP_VERSION)
    app.setWindowIcon(load_application_icon())

    try:
        database = Database(db_path)
        database.initialize()
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        QMessageBox.critical(
            None,
            "Ошибка локальной базы данных",
            f"Приложение не может открыть локальную базу данных.\n\n{exc}\n\n"
            "Проверьте доступность каталога данных. Существующая база не сбрасывалась.",
        )
        return 1

    try:
        # UI bootstrap also reads SQLite (performer/settings and initial cards).
        # A failed read must not leave an empty, editable screen running.
        window = MainWindow(database)
    except (OSError, sqlite3.Error, RuntimeError, LookupError) as exc:
        QMessageBox.critical(
            None,
            "Ошибка загрузки интерфейса",
            f"Не удалось загрузить данные приложения. Окно не открыто.\n\n{exc}\n\n"
            "Проверьте доступность базы данных и запустите приложение повторно. "
            "Существующая база не сбрасывалась.",
        )
        return 1
    window.show()
    return app.exec()


if __name__ == "__main__":
    if "--release-smoke" in sys.argv:
        # An explicit, isolated QA entrypoint for exercising the *frozen* executable.
        # It refuses to run without a dedicated test profile (see release_smoke.py).
        from app.release_smoke import run_release_smoke
        raise SystemExit(run_release_smoke())
    raise SystemExit(main())
