"""The app must ship and render the ORIGINAL user-supplied icon assets."""
from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path
import struct
import unittest

ASSETS = Path(__file__).resolve().parents[1] / "app" / "assets"
ORIGINAL_SHA256 = {
    "kontakt-icon.ico": "c636e4e493928df429142e464a76d61d9086f5f2784f745f168b4ad74f7f798f",
    "kontakt-icon.svg": "40737e7b2c4f082d2dd6d63e348aaf4ec01408163d3deba3a674fdf71dce1d4e",
}
HAS_QT = importlib.util.find_spec("PySide6") is not None
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class UploadedIconAssetTests(unittest.TestCase):
    def test_uploaded_ico_and_svg_bytes_are_unchanged(self):
        for name, expected_sha256 in ORIGINAL_SHA256.items():
            with self.subTest(name=name):
                content = (ASSETS / name).read_bytes()
                self.assertEqual(hashlib.sha256(content).hexdigest(), expected_sha256)

    def test_git_preserves_original_icon_bytes_on_windows(self):
        attributes = (ASSETS.parents[1] / ".gitattributes").read_text(encoding="utf-8")
        self.assertIn("app/assets/kontakt-icon.ico binary", attributes)
        self.assertIn("app/assets/kontakt-icon.svg -text", attributes)

    def test_ico_contains_seven_original_windows_resolutions(self):
        content = (ASSETS / "kontakt-icon.ico").read_bytes()
        self.assertEqual(content[:4], b"\x00\x00\x01\x00")
        count = struct.unpack_from("<H", content, 4)[0]
        self.assertEqual(count, 7)

    @unittest.skipUnless(HAS_QT, "PySide6 not installed")
    def test_qt_can_render_uploaded_ico_for_window_title(self):
        from PySide6.QtWidgets import QApplication
        from app.app_icon import application_icon_path, load_application_icon

        instance = QApplication.instance() or QApplication([])
        self.assertEqual(application_icon_path(), ASSETS / "kontakt-icon.ico")
        icon = load_application_icon()
        self.assertFalse(icon.isNull())
        self.assertFalse(icon.pixmap(32, 32).isNull())
        instance.setWindowIcon(icon)
        self.assertFalse(instance.windowIcon().isNull())


if __name__ == "__main__":
    unittest.main()
