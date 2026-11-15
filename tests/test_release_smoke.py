"""Release probe tests use subprocesses and a synthetic dedicated user profile."""
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

HAS_QT = importlib.util.find_spec("PySide6") is not None


@unittest.skipUnless(HAS_QT, "PySide6 is required to execute the real UI")
class ReleaseSmokeTests(unittest.TestCase):
    def test_frozen_entrypoint_requires_explicit_isolated_profile(self):
        from app.release_smoke import run_release_smoke
        with mock.patch.dict(os.environ, {"KONTAKT_RELEASE_SMOKE": "0"}):
            self.assertEqual(run_release_smoke(), 2)

    def test_qa_appearance_is_ini_only_and_requires_matching_test_database(self):
        from app.ui.common import appearance_settings, read_appearance
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            expected = root / "ElectricityReports" / "app.db"
            environment = {
                "KONTAKT_RELEASE_SMOKE": "1",
                "KONTAKT_SMOKE_ROOT": str(root),
            }
            with mock.patch.dict(os.environ, environment), mock.patch(
                "app.ui.common.get_database_path", return_value=expected
            ):
                appearance = appearance_settings()
                self.assertEqual(Path(appearance.fileName()).resolve(),
                                 root / "qa-appearance.ini")
                self.assertEqual(read_appearance(), (100, 400))
                appearance.setValue("font_size_percent", 125)
                appearance.setValue("font_weight_value", 700)
                appearance.sync()
                self.assertEqual(read_appearance(), (125, 700))
                self.assertTrue((root / "qa-appearance.ini").is_file())
            with mock.patch.dict(os.environ, environment), mock.patch(
                "app.ui.common.get_database_path",
                return_value=root / "Unrelated" / "app.db"
            ):
                with self.assertRaisesRegex(RuntimeError, "не соответствует"):
                    appearance_settings()

    def test_source_probe_runs_twice_without_touching_real_profile(self):
        with tempfile.TemporaryDirectory(prefix="kontakt-synthetic-") as directory:
            root = Path(directory)
            local = root / "Изолированный профиль"
            local.mkdir()
            marker = root / "result.txt"
            env = os.environ.copy()
            env.update({
                "KONTAKT_RELEASE_SMOKE": "1",
                "KONTAKT_SMOKE_ROOT": str(local),
                "KONTAKT_SMOKE_RESULT": str(marker),
                "LOCALAPPDATA": str(local),
                "XDG_DATA_HOME": str(local),
                "QT_QPA_PLATFORM": "offscreen",
            })
            for expected in ("PASS fresh", "PASS existing"):
                completed = subprocess.run(
                    [sys.executable, "-m", "app.main", "--release-smoke"],
                    env=env, cwd=Path(__file__).resolve().parents[1],
                    capture_output=True, text=True, timeout=120,
                )
                report = marker.read_text(encoding="utf-8") if marker.exists() else ""
                self.assertEqual(completed.returncode, 0, report + completed.stderr)
                self.assertTrue(report.startswith(expected), report)
            self.assertTrue((local / "ElectricityReports" / "app.db").is_file())
            self.assertTrue((local / "qa-appearance.ini").is_file())


if __name__ == "__main__":
    unittest.main()
