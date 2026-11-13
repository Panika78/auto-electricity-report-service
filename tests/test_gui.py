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

    def test_main_window_opens_and_can_close(self):
        from app.ui.main_window import MainWindow

        window = MainWindow()
        window.show()
        self.assertTrue(window.isVisible())
        self.assertIn("Контакт", window.windowTitle())
        window.close()
        self.assertFalse(window.isVisible())

    def test_startup_initializes_database_before_opening_window(self):
        from PySide6.QtWidgets import QApplication
        from app.main import main

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.db"
            with mock.patch.object(QApplication, "exec", return_value=0):
                self.assertEqual(main(db_path=path), 0)
            self.assertTrue(path.is_file())

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


if __name__ == "__main__":
    unittest.main()
