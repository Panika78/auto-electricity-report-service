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
        from app.qa_profile import create_qa_profile
        from app.ui.common import appearance_settings, read_appearance
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            expected = root / "ElectricityReports" / "app.db"
            token = create_qa_profile(root)
            environment = {
                "KONTAKT_RELEASE_SMOKE": "1",
                "KONTAKT_SMOKE_ROOT": str(root),
                "KONTAKT_SMOKE_TOKEN": token,
            }
            with mock.patch.dict(os.environ, environment), mock.patch(
                "app.qa_profile.db.get_database_path", return_value=expected
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
                "app.qa_profile.db.get_database_path",
                return_value=root / "Unrelated" / "app.db"
            ):
                with self.assertRaisesRegex(RuntimeError, "не соответствует"):
                    appearance_settings()

    def test_matching_paths_without_marker_fail_before_opening_sqlite(self):
        with tempfile.TemporaryDirectory(prefix="kontakt-no-marker-") as directory:
            root = Path(directory).resolve()
            local = root / "qa"
            local.mkdir()
            victim = root / "victim.txt"
            victim.write_bytes(b"ORIGINAL")
            env = os.environ.copy()
            env.update({
                "KONTAKT_RELEASE_SMOKE": "1",
                "KONTAKT_SMOKE_ROOT": str(local),
                "KONTAKT_SMOKE_TOKEN": "0" * 64,
                "KONTAKT_SMOKE_RESULT": str(victim),
                "LOCALAPPDATA": str(local),
                "XDG_DATA_HOME": str(local),
                "QT_QPA_PLATFORM": "offscreen",
            })
            result = subprocess.run(
                [sys.executable, "-m", "app.main", "--release-smoke"],
                env=env, cwd=Path(__file__).resolve().parents[1],
                capture_output=True, text=True, timeout=30,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(victim.read_bytes(), b"ORIGINAL")
            self.assertFalse((local / "ElectricityReports" / "app.db").exists())

    def test_existing_report_is_never_truncated_with_valid_profile(self):
        from app.qa_profile import create_qa_profile
        with tempfile.TemporaryDirectory(prefix="kontakt-existing-report-") as directory:
            local = Path(directory).resolve()
            token = create_qa_profile(local)
            victim = local / ("qa-result-" + "f" * 32 + ".txt")
            victim.write_bytes(b"ORIGINAL REPORT")
            env = os.environ.copy()
            env.update({
                "KONTAKT_RELEASE_SMOKE": "1",
                "KONTAKT_SMOKE_ROOT": str(local),
                "KONTAKT_SMOKE_TOKEN": token,
                "KONTAKT_SMOKE_RESULT": str(victim),
                "LOCALAPPDATA": str(local),
                "XDG_DATA_HOME": str(local),
                "QT_QPA_PLATFORM": "offscreen",
            })
            completed = subprocess.run(
                [sys.executable, "-m", "app.main", "--release-smoke"],
                env=env, cwd=Path(__file__).resolve().parents[1],
                capture_output=True, text=True, timeout=30,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertEqual(victim.read_bytes(), b"ORIGINAL REPORT")
            self.assertFalse((local / "ElectricityReports" / "app.db").exists())

    def test_valid_profile_does_not_authorize_external_report(self):
        from app.qa_profile import create_qa_profile
        with tempfile.TemporaryDirectory(prefix="kontakt-external-report-") as directory:
            root = Path(directory).resolve()
            local = root / "profile"
            local.mkdir()
            token = create_qa_profile(local)
            victim = root / ("qa-result-" + "a" * 32 + ".txt")
            victim.write_bytes(b"EXTERNAL")
            env = os.environ.copy()
            env.update({
                "KONTAKT_RELEASE_SMOKE": "1",
                "KONTAKT_SMOKE_ROOT": str(local),
                "KONTAKT_SMOKE_TOKEN": token,
                "KONTAKT_SMOKE_RESULT": str(victim),
                "LOCALAPPDATA": str(local),
                "XDG_DATA_HOME": str(local),
                "QT_QPA_PLATFORM": "offscreen",
            })
            completed = subprocess.run(
                [sys.executable, "-m", "app.main", "--release-smoke"],
                env=env, cwd=Path(__file__).resolve().parents[1],
                capture_output=True, text=True, timeout=30,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertEqual(victim.read_bytes(), b"EXTERNAL")
            self.assertFalse((local / "ElectricityReports" / "app.db").exists())

    def test_rejects_actual_system_profile_even_with_valid_marker(self):
        from app.qa_profile import create_qa_profile, validate_qa_profile
        with tempfile.TemporaryDirectory(prefix="kontakt-system-profile-") as directory:
            root = Path(directory).resolve()
            token = create_qa_profile(root)
            env = {
                "KONTAKT_RELEASE_SMOKE": "1",
                "KONTAKT_SMOKE_ROOT": str(root),
                "KONTAKT_SMOKE_TOKEN": token,
                "LOCALAPPDATA": str(root),
                "XDG_DATA_HOME": str(root),
            }
            with mock.patch.dict(os.environ, env), mock.patch(
                "app.qa_profile._system_profile_paths", return_value=(root,)
            ):
                with self.assertRaisesRegex(RuntimeError, "системном профиле"):
                    validate_qa_profile()
            self.assertFalse((root / "ElectricityReports" / "app.db").exists())

    def test_source_probe_runs_twice_without_touching_real_profile(self):
        with tempfile.TemporaryDirectory(prefix="kontakt-synthetic-") as directory:
            root = Path(directory)
            local = root / "Изолированный профиль"
            local.mkdir()
            from app.qa_profile import create_qa_profile
            token = create_qa_profile(local)
            env = os.environ.copy()
            env.update({
                "KONTAKT_RELEASE_SMOKE": "1",
                "KONTAKT_SMOKE_ROOT": str(local),
                "KONTAKT_SMOKE_TOKEN": token,
                "LOCALAPPDATA": str(local),
                "XDG_DATA_HOME": str(local),
                "QT_QPA_PLATFORM": "offscreen",
            })
            for index, expected in enumerate(("PASS fresh", "PASS existing"), start=1):
                marker = local / ("qa-result-" + str(index) * 32 + ".txt")
                env["KONTAKT_SMOKE_RESULT"] = str(marker)
                completed = subprocess.run(
                    [sys.executable, "-m", "app.main", "--release-smoke"],
                    env=env, cwd=Path(__file__).resolve().parents[1],
                    capture_output=True, text=True, timeout=120,
                )
                report = marker.read_text(encoding="utf-8") if marker.exists() else ""
                self.assertEqual(completed.returncode, 0, report + completed.stderr)
                self.assertTrue(report.startswith(expected), report)
            first = local / ("qa-result-" + "1" * 32 + ".txt")
            self.assertEqual(first.read_text(encoding="utf-8"), "PASS fresh\n")
            self.assertTrue((local / "ElectricityReports" / "app.db").is_file())
            self.assertTrue((local / "qa-appearance.ini").is_file())


if __name__ == "__main__":
    unittest.main()
