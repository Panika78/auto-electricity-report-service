"""Короткие GUI-проверки; выполняются при наличии PySide6."""

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HAS_QT = importlib.util.find_spec("PySide6") is not None


@unittest.skipUnless(HAS_QT, "PySide6 недоступен в среде тестирования")
class GuiSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        """Release hidden Qt top-levels before QApplication interpreter shutdown.

        Qt's offscreen platform can abort in C++ virtual destruction when
        Python wrappers for long-lived dialogs are finalized after the app.
        """
        import gc
        from PySide6.QtCore import QCoreApplication, QEvent
        from PySide6.QtWidgets import QApplication
        for widget in list(QApplication.topLevelWidgets()):
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QApplication.processEvents()
        gc.collect()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def setUp(self):
        # Fail hard if a smoke test ever falls back to the real profile database.
        from app.data import database as db
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        guard = mock.patch.object(
            db, "get_database_path",
            side_effect=AssertionError("GUI smoke tests must never open the user database"))
        self.default_database_path = guard.start()
        self.addCleanup(guard.stop)

    def test_main_window_opens_and_can_close(self):
        from app.data.database import Database
        from app.ui.main_window import MainWindow

        database = Database(Path(self.temporary.name) / "smoke.db")
        database.initialize()
        window = MainWindow(database)
        try:
            window.show()
            self.assertTrue(window.isVisible())
            self.assertTrue(window.windowTitle().startswith("контакт"))
        finally:
            window.close()
        self.assertFalse(window.isVisible())
        self.assertTrue(database.path.is_file())
        self.default_database_path.assert_not_called()

    def test_startup_initializes_database_before_opening_window(self):
        from PySide6.QtWidgets import QApplication
        from app.main import main

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.db"
            with mock.patch.object(QApplication, "exec", return_value=0):
                self.assertEqual(main(db_path=path), 0)
            self.assertEqual(QApplication.applicationName(), "контакт")
            self.assertTrue(path.is_file())
            self.default_database_path.assert_not_called()

    def test_startup_initial_settings_failure_does_not_show_empty_window(self):
        import sqlite3
        from PySide6.QtWidgets import QApplication, QMessageBox
        from app.main import main
        path = Path(self.temporary.name) / "settings-failure.db"
        with mock.patch("app.ui.main_window.SettingsPage",
                        side_effect=sqlite3.OperationalError("database is locked")), \
             mock.patch.object(QMessageBox, "critical") as critical, \
             mock.patch.object(QApplication, "exec",
                               side_effect=AssertionError("Startup must stop on load failure")) as run:
            self.assertEqual(main(db_path=path), 1)
            critical.assert_called_once()
            self.assertEqual(critical.call_args.args[1], "Ошибка загрузки интерфейса")
            self.assertIn("database is locked", critical.call_args.args[2])
            run.assert_not_called()
        self.assertTrue(path.is_file())
        self.default_database_path.assert_not_called()

    def test_startup_initialization_error_is_reported(self):
        from PySide6.QtWidgets import QMessageBox
        from app.main import main
        with tempfile.TemporaryDirectory() as directory:
            blocked = Path(directory) / "file"
            blocked.write_text("not a directory")
            with mock.patch.object(QMessageBox, "critical") as critical:
                self.assertEqual(main(db_path=blocked / "app.db"), 1)
                critical.assert_called_once()
                self.assertIn("Ошибка", critical.call_args.args[1])
        self.default_database_path.assert_not_called()


    def test_queued_column_fit_is_destroyed_with_window(self):
        # A queued QTimer callback must not outlive its QTableWidget on Windows.
        from shiboken6 import isValid
        from PySide6.QtCore import QCoreApplication, QEvent
        from PySide6.QtWidgets import QApplication
        from app.data.database import Database
        from app.ui.main_window import MainWindow

        database = Database(Path(self.temporary.name) / "fit.db")
        database.initialize()
        window = MainWindow(database)
        window.show()
        window._schedule_calculation_fit()
        queued_timer = window._immediate_fit_timer
        self.assertTrue(queued_timer.isActive())
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QApplication.processEvents()
        self.assertFalse(isValid(queued_timer))
        self.default_database_path.assert_not_called()


if __name__ == "__main__":
    unittest.main()
