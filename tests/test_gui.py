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


if __name__ == "__main__":
    unittest.main()
