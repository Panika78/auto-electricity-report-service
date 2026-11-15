"""Verify that a release manifest describes the exact delivered inner ZIP."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


def validate_manifest(manifest_path: Path, archive_path: Path, source_commit: str | None = None) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if manifest.get("format_version") != 1:
        raise ValueError("Unsupported release manifest version.")
    if archive_path.name != manifest.get("zip_filename"):
        raise ValueError("Release manifest references a different ZIP.")
    actual = hashlib.sha256(archive_path.read_bytes()).hexdigest().upper()
    if manifest.get("zip_sha256", "").upper() != actual:
        raise ValueError("Release ZIP hash does not match its manifest.")
    commit = manifest.get("source_commit", "")
    if not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("Release source commit is absent or malformed.")
    if source_commit is not None and commit != source_commit.lower():
        raise ValueError("Release manifest refers to a different source commit.")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--zip", required=True, type=Path)
    parser.add_argument("--source-commit")
    arguments = parser.parse_args()
    info = validate_manifest(arguments.manifest, arguments.zip, arguments.source_commit)
    print(f"Release manifest verified: {info['source_commit']} {info['zip_sha256']}")


if __name__ == "__main__":
    main()
