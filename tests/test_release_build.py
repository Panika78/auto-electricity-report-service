"""Security and manifest regression tests for synthetic Windows onedir files."""
import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.check_release_manifest import validate_manifest
from scripts.validate_windows_bundle import (
    PDF_RESOURCES, UnsafeBundleError, validate_directory, validate_zip,
)


def create_fake_distribution(root: Path) -> Path:
    bundle = root / "kontakt"
    names = {
        "kontakt.exe", "_internal/base_library.zip", "_internal/python311.dll",
        "_internal/PySide6/Qt/plugins/platforms/qwindows.dll",
        "_internal/PySide6/Qt/plugins/platforms/qoffscreen.dll",
    } | {f"_internal/app/documents/fonts/{name}" for name in PDF_RESOURCES}
    for name in names:
        file = bundle / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b"synthetic file")
    return bundle


class WindowsReleaseStructureTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="kontakt-build-test-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.dist = create_fake_distribution(self.root)

    def test_known_runtime_and_pdf_files_are_allowed(self):
        names = validate_directory(self.dist)
        self.assertIn("_internal/base_library.zip", names)
        self.assertIn("_internal/app/documents/fonts/OFL-PTSans.txt", names)

    def test_personal_files_blocked_even_with_innocent_extension_or_filename(self):
        for name in ("clients.csv", "bank-details.txt", "private-scan.png",
                     "notes.json", "data.backup", "secret.jpeg"):
            with self.subTest(name=name):
                file = self.dist / "_internal" / name
                file.write_bytes(b"synthetic personal data")
                try:
                    with self.assertRaisesRegex(UnsafeBundleError, "разрешённый"):
                        validate_directory(self.dist)
                finally:
                    file.unlink()

    def test_unexpected_files_outside_internal_are_rejected(self):
        path = self.dist / "developer-notes.md"
        path.write_text("private", encoding="utf-8")
        with self.assertRaises(UnsafeBundleError):
            validate_directory(self.dist)

    def test_unknown_pdf_font_rejected(self):
        path = self.dist / "_internal/app/documents/fonts/private-photo.png"
        path.write_bytes(b"synthetic")
        with self.assertRaises(UnsafeBundleError):
            validate_directory(self.dist)

    def test_rejects_missing_required_qt_plugin(self):
        (self.dist / "_internal/PySide6/Qt/plugins/platforms/qwindows.dll").unlink()
        with self.assertRaisesRegex(UnsafeBundleError, "Qt platform"):
            validate_directory(self.dist)

    def test_release_manifest_is_bound_to_inner_zip_and_git_commit(self):
        archive = self.root / "kontakt-windows-x64.zip"
        archive.write_bytes(b"synthetic release ZIP")
        manifest = self.root / "release-manifest.json"
        commit = "1" * 40
        data = {
            "format_version": 2,
            "build_checkout_commit": commit,
            "build_tree_sha": "3" * 40,
            "pr_head_commit": "4" * 40,
            "zip_filename": archive.name,
            "zip_sha256": hashlib.sha256(archive.read_bytes()).hexdigest().upper(),
        }
        manifest.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(
            validate_manifest(manifest, archive, commit, "4" * 40, "3" * 40)
            ["build_checkout_commit"], commit
        )
        archive.write_bytes(b"synthetic replacement ZIP")
        with self.assertRaisesRegex(ValueError, "hash"):
            validate_manifest(manifest, archive, commit)
        archive.write_bytes(b"synthetic release ZIP")
        with self.assertRaisesRegex(ValueError, "build checkout"):
            validate_manifest(manifest, archive, "2" * 40)
        with self.assertRaisesRegex(ValueError, "PR head"):
            validate_manifest(manifest, archive, commit, "5" * 40)
        with self.assertRaisesRegex(ValueError, "source tree"):
            validate_manifest(manifest, archive, commit, "4" * 40, "6" * 40)
        data["build_checkout_commit"] = "2" * 40
        manifest.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "build checkout"):
            validate_manifest(manifest, archive, commit)
        data.pop("build_tree_sha")
        manifest.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "build tree"):
            validate_manifest(manifest, archive)

    def test_zip_must_match_exact_ondir_contents(self):
        original = validate_directory(self.dist)
        path = self.root / "kontakt-windows-x64.zip"
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zipfile_:
            for f in self.dist.rglob("*"):
                if f.is_file():
                    zipfile_.write(f, arcname="kontakt/" + f.relative_to(self.dist).as_posix())
        self.assertEqual(validate_zip(path, original), original)
        with zipfile.ZipFile(path, "a") as zipfile_:
            zipfile_.writestr("kontakt/_internal/bank-details.txt", "fake")
        with self.assertRaisesRegex(UnsafeBundleError, "разрешённый"):
            validate_zip(path, original)


if __name__ == "__main__":
    unittest.main()
