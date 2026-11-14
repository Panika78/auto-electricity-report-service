"""Запуск настольного приложения: python -m app.main."""

import sqlite3
from pathlib import Path

from app.data.database import Database


def main(db_path: Path | str | None = None) -> int:
    # Импорт GUI при запуске, чтобы слой данных можно было тестировать без Qt.
    from PySide6.QtWidgets import QApplication, QMessageBox
    from app.ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    app.setApplicationName("Контакт")

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

    window = MainWindow(database)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
