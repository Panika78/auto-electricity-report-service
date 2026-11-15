"""Verify that a release manifest describes the exact delivered inner ZIP."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


def validate_manifest(
    manifest_path: Path, archive_path: Path,
    build_checkout_commit: str | None = None,
    pr_head_commit: str | None = None,
    build_tree_sha: str | None = None,
) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if manifest.get("format_version") != 2:
        raise ValueError("Unsupported release manifest version.")
    if archive_path.name != manifest.get("zip_filename"):
        raise ValueError("Release manifest references a different ZIP.")
    actual = hashlib.sha256(archive_path.read_bytes()).hexdigest().upper()
    if manifest.get("zip_sha256", "").upper() != actual:
        raise ValueError("Release ZIP hash does not match its manifest.")
    commit = manifest.get("build_checkout_commit")
    tree = manifest.get("build_tree_sha")
    head = manifest.get("pr_head_commit")
    if not isinstance(commit, str) or not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("Release build checkout commit is absent or malformed.")
    if not isinstance(tree, str) or not re.fullmatch(r"[a-f0-9]{40}", tree):
        raise ValueError("Release build tree SHA is absent or malformed.")
    if "pr_head_commit" not in manifest or (
        head is not None and (not isinstance(head, str) or not re.fullmatch(r"[a-f0-9]{40}", head))
    ):
        raise ValueError("Release PR head commit is malformed.")
    if build_checkout_commit is not None and commit != build_checkout_commit.lower():
        raise ValueError("Release manifest refers to a different build checkout commit.")
    if build_tree_sha is not None and tree != build_tree_sha.lower():
        raise ValueError("Release manifest refers to a different source tree.")
    if pr_head_commit is not None and head != pr_head_commit.lower():
        raise ValueError("Release manifest refers to a different PR head commit.")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--zip", required=True, type=Path)
    parser.add_argument("--build-checkout-commit")
    parser.add_argument("--pr-head-commit")
    parser.add_argument("--build-tree-sha")
    arguments = parser.parse_args()
    info = validate_manifest(
        arguments.manifest, arguments.zip,
        arguments.build_checkout_commit, arguments.pr_head_commit,
        arguments.build_tree_sha,
    )
    print(f"Release manifest verified: checkout={info['build_checkout_commit']} "
          f"tree={info['build_tree_sha']} pr_head={info['pr_head_commit']} "
          f"zip={info['zip_sha256']}")


if __name__ == "__main__":
    main()
