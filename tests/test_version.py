"""Release version identity must match packaging, GUI and release notes."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import tempfile
import tomllib
import unittest

from app.version import APP_VERSION

ROOT = Path(__file__).resolve().parents[1]
HAS_QT = importlib.util.find_spec("PySide6") is not None
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class StableVersionTests(unittest.TestCase):
    def test_project_metadata_matches_application_version(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(APP_VERSION, "1.0.0")
        self.assertEqual(project["project"]["version"], APP_VERSION)

    def test_release_notes_and_readme_have_same_version(self):
        for name in ("README.md", "CHANGELOG.md"):
            with self.subTest(name=name):
                self.assertIn("v" + APP_VERSION, (ROOT / name).read_text(encoding="utf-8"))

    def test_release_workflow_runs_when_main_version_is_published(self):
        workflow = (ROOT / ".github/workflows/windows-release.yml").read_text(encoding="utf-8")
        before_jobs = workflow.split("\njobs:", 1)[0]
        self.assertIn('push:\n    branches: [main]\n    paths:\n      - "pyproject.toml"', before_jobs)
        self.assertIn("  release:", workflow)
        self.assertIn("gh release create", workflow)

    @unittest.skipUnless(HAS_QT, "PySide6 is not installed")
    def test_settings_show_real_release_version(self):
        from PySide6.QtWidgets import QApplication, QLabel
        from app.core.service import CalculationService
        from app.data.database import Database
        from app.ui.forms import SettingsPage

        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory(prefix="kontakt-version-") as directory:
            database = Database(Path(directory) / "app.db")
            database.initialize()
            settings = SettingsPage(CalculationService(database))
            try:
                label = settings.findChild(QLabel, "appVersion")
                self.assertIsNotNone(label)
                self.assertIn(APP_VERSION, label.text())
            finally:
                settings.close()
                settings.deleteLater()
        self.assertIsNotNone(app)
