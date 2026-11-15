"""Run the actual windowed onedir EXE twice in an isolated synthetic profile.

Windows CI is Windows Server, not a substitute for Windows 10/11 manual tests.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_bundle(dist: Path) -> None:
    if sys.platform != "win32":
        raise RuntimeError("Frozen executable integration test requires Windows.")
    dist = dist.resolve()
    if not (dist / "kontakt.exe").is_file():
        raise FileNotFoundError("Не найден dist/kontakt/kontakt.exe.")

    with tempfile.TemporaryDirectory(prefix="kontakt-qa-") as directory:
        root = Path(directory)
        target = root / "Папка с пробелами и кириллицей" / "контакт"
        target.parent.mkdir(parents=True)
        profile = root / "Тестовый профиль" / "Local"
        profile.mkdir(parents=True)
        roaming = root / "Тестовый профиль" / "Roaming"
        roaming.mkdir(parents=True)
        expected_database = profile / "ElectricityReports" / "app.db"
        marker = root / "probe.txt"
        image_hash = None

        env = dict(os.environ)
        env.update({
            "LOCALAPPDATA": str(profile),
            "APPDATA": str(roaming),
            "KONTAKT_SMOKE_ROOT": str(profile),
            "KONTAKT_SMOKE_RESULT": str(marker),
            "KONTAKT_RELEASE_SMOKE": "1",
            "QT_QPA_PLATFORM": "offscreen",
            "QT_OPENGL": "software",
        })
        # Withhold Python and third-party executables from the process search PATH.
        # The runner still has Python installed; this is NOT a clean-VM test.
        windows = Path(os.environ.get("SystemRoot", "C:\\Windows"))
        env["PATH"] = str(windows / "System32") + os.pathsep + str(windows)

        for index, expected in enumerate(("PASS fresh", "PASS existing")):
            if target.exists():
                # Simulate replacing the program directory, NEVER the profile.
                shutil.rmtree(target)
            shutil.copytree(dist, target)
            marker.unlink(missing_ok=True)
            result = subprocess.run(
                [str(target / "kontakt.exe"), "--release-smoke"],
                env=env, cwd=target.parent, timeout=120,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, errors="replace", check=False,
            )
            report = marker.read_text(encoding="utf-8") if marker.is_file() else "(no probe result)"
            if result.returncode != 0 or not report.startswith(expected):
                raise AssertionError(
                    f"Frozen EXE probe {index + 1} failed ({result.returncode}): "
                    f"{report}\n{result.stderr[-2000:]}"
                )
            if not expected_database.is_file():
                raise AssertionError("Frozen EXE did not create the default LOCALAPPDATA SQLite file.")
            # sqlite3.Connection.__exit__ commits but does NOT close the handle.
            # On Windows that prevents deleting the disposable test profile.
            with closing(sqlite3.connect(expected_database)) as connection:
                if connection.execute("SELECT count(*) FROM tenants").fetchone()[0] != 1:
                    raise AssertionError("Tenant record lost between distribution replacements.")
                if connection.execute("SELECT count(*) FROM documents").fetchone()[0] != 2:
                    raise AssertionError("PDF metadata lost between distribution replacements.")
            images = list((expected_database.parent / "images").glob("signature-*.png"))
            if len(images) != 1:
                raise AssertionError("Managed synthetic signature lost or duplicated.")
            new_hash = _sha256(images[0])
            if image_hash is not None and image_hash != new_hash:
                raise AssertionError("Synthetic signature changed when program directory was replaced.")
            image_hash = new_hash
            print(f"Frozen EXE verification: {expected}, real Qt + PDF + ZIP, isolated profile.")

        print("Release probe complete. This does NOT certify a clean Windows 10/11 machine.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", required=True, type=Path)
    args = parser.parse_args()
    check_bundle(args.dist)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
