"""Run the actual windowed onedir EXE twice in an isolated synthetic profile.

Windows CI is Windows Server, not a substitute for Windows 10/11 manual tests.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import time
from contextlib import closing
import hashlib
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from app.qa_profile import create_qa_profile


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _visible_kontakt_window(pid: int) -> int | None:
    """Look up a visible native Win32 top-level window of the actual EXE."""
    user32 = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    windows: list[int] = []

    @callback_type
    def visit(hwnd, _):
        target_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(target_pid))
        if target_pid.value == pid and user32.IsWindowVisible(hwnd):
            title = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, title, len(title))
            if title.value.startswith("контакт"):
                windows.append(hwnd)
        return True

    if not user32.EnumWindows(visit, 0):
        raise OSError("Не удалось перечислить окна Win32.")
    return windows[0] if windows else None


def _test_normal_gui_startup(executable: Path, root: Path, environment: dict[str, str]) -> None:
    """Use the normal startup path and Qt 'windows' platform, not --release-smoke."""
    profile = root / "Обычный GUI профиль" / "Local"
    profile.mkdir(parents=True)
    roaming = root / "Обычный GUI профиль" / "Roaming"
    roaming.mkdir(parents=True)
    token = create_qa_profile(profile)
    env = dict(environment)
    env.update({
        "LOCALAPPDATA": str(profile),
        "APPDATA": str(roaming),
        "KONTAKT_SMOKE_ROOT": str(profile),
        "KONTAKT_SMOKE_TOKEN": token,
        # Only QSettings is redirected for safety; main() runs unchanged.
        "QT_QPA_PLATFORM": "windows",
    })
    env.pop("KONTAKT_SMOKE_RESULT", None)
    process = subprocess.Popen(
        [str(executable)], env=env, cwd=executable.parent,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    try:
        until = time.monotonic() + 25
        hwnd = None
        while time.monotonic() < until:
            if process.poll() is not None:
                raise AssertionError("Normal Windows GUI process exited before showing its window.")
            hwnd = _visible_kontakt_window(process.pid)
            if hwnd is not None:
                break
            time.sleep(0.2)
        if hwnd is None:
            raise AssertionError("Normal Qt Windows GUI did not create a visible 'контакт' window.")
        # Close just the observed window. Never terminate a user-owned process.
        user32 = ctypes.windll.user32
        user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.PostMessageW.restype = wintypes.BOOL
        if not user32.PostMessageW(hwnd, 0x0010, 0, 0):  # WM_CLOSE
            raise OSError("Windows не принял сообщение закрытия окна.")
        code = process.wait(timeout=15)
        if code != 0:
            raise AssertionError(f"Normal Windows GUI exited with code {code}.")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        if process.stderr is not None:
            process.stderr.close()

    database = profile / "ElectricityReports" / "app.db"
    if not database.is_file():
        raise AssertionError("Normal startup did not create its own SQLite profile.")
    with closing(sqlite3.connect(database)) as connection:
        if connection.execute("SELECT COUNT(*) FROM tenants").fetchone()[0] != 0:
            raise AssertionError("Normal startup modified the synthetic tenants unexpectedly.")
    print("Normal Windows GUI startup: PASS, qwindows.dll, visible native window, WM_CLOSE.")


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
        token = create_qa_profile(profile)
        expected_database = profile / "ElectricityReports" / "app.db"
        image_hash = None

        env = dict(os.environ)
        env.update({
            "LOCALAPPDATA": str(profile),
            "APPDATA": str(roaming),
            "KONTAKT_SMOKE_ROOT": str(profile),
            "KONTAKT_SMOKE_TOKEN": token,
            "KONTAKT_RELEASE_SMOKE": "1",
            "QT_QPA_PLATFORM": "offscreen",
            "QT_OPENGL": "software",
        })
        # Withhold Python and third-party executables from the process search PATH.
        # The runner still has Python installed; this is NOT a clean-VM test.
        windows = Path(os.environ.get("SystemRoot", "C:\\Windows"))
        env["PATH"] = str(windows / "System32") + os.pathsep + str(windows)

        for index, expected in enumerate(("PASS fresh", "PASS existing")):
            marker = profile / f"qa-result-{secrets.token_hex(16)}.txt"
            env["KONTAKT_SMOKE_RESULT"] = str(marker)
            if target.exists():
                # Simulate replacing the program directory, NEVER the profile.
                shutil.rmtree(target)
            shutil.copytree(dist, target)
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

        _test_normal_gui_startup(target / "kontakt.exe", root, env)
        print("Release probe complete. This does NOT certify a clean Windows 10/11 machine.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", required=True, type=Path)
    args = parser.parse_args()
    check_bundle(args.dist)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
