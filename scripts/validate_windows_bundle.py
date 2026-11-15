"""Reject unexpected assets in an onedir distribution and its delivered ZIP.

This is a strict *resource-type/location* allowlist, not a malware scanner or
proof that arbitrary binary files contain no personal information. The release
must always be built from reviewed source and an isolated clean environment.
"""
from __future__ import annotations

import argparse
import zipfile
from pathlib import Path, PurePosixPath

PDF_RESOURCES = {
    "PT_Sans-Web-Regular.ttf",
    "PT_Sans-Web-Bold.ttf",
    "PT_Serif-Web-Regular.ttf",
    "PT_Serif-Web-Bold.ttf",
    "OFL-PTSans.txt",
    "OFL-PTSerif.txt",
}
REQUIRED_DLLS = {"python311.dll"}
QT_PLUGINS = {"qwindows.dll", "qoffscreen.dll"}
BINARY_SUFFIXES = {".dll", ".pyd"}
# Only Qt's immutable runtime data is eligible for non-binary resource types.
QT_DATA_SUFFIXES = {".qm", ".pak", ".dat", ".rcc"}
QT_RESOURCE_ROOTS = {
    ("PySide6", "translations"),
    ("PySide6", "Qt", "translations"),
    ("PySide6", "Qt", "resources"),
}


class UnsafeBundleError(ValueError):
    pass


def _allowed(relative: PurePosixPath) -> bool:
    parts = relative.parts
    if str(relative) == "kontakt.exe":
        return True
    if not parts or parts[0] != "_internal":
        return False
    inside = parts[1:]
    if not inside:
        return False
    if tuple(inside) == ("base_library.zip",):
        return True
    if inside[:3] == ("app", "documents", "fonts"):
        return len(inside) == 4 and inside[3] in PDF_RESOURCES
    if relative.suffix.lower() in BINARY_SUFFIXES:
        # PyInstaller's analyzed Python/Qt DLLs and extension modules.
        return True
    if any(inside[:len(prefix)] == prefix and len(inside) > len(prefix)
           for prefix in QT_RESOURCE_ROOTS):
        return relative.suffix.lower() in QT_DATA_SUFFIXES
    return False


def _require_safe(relative: PurePosixPath) -> None:
    if (not relative.parts or relative.is_absolute()
            or ".." in relative.parts or "." in relative.parts
            or "\\" in str(relative) or not _allowed(relative)):
        raise UnsafeBundleError(f"Файл не входит в разрешённый состав поставки: {relative}")


def validate_entries(names: set[str]) -> None:
    if not names:
        raise UnsafeBundleError("Поставка пуста.")
    for name in names:
        relative = PurePosixPath(name)
        _require_safe(relative)
    if "kontakt.exe" not in names:
        raise UnsafeBundleError("В поставке нет kontakt.exe.")
    if "_internal/base_library.zip" not in names:
        raise UnsafeBundleError("В поставке нет стандартной библиотеки Python.")
    for font in PDF_RESOURCES:
        if f"_internal/app/documents/fonts/{font}" not in names:
            raise UnsafeBundleError(f"Отсутствует обязательный ресурс PDF: {font}.")
    for name in REQUIRED_DLLS:
        if not any(PurePosixPath(item).name.lower() == name.lower() for item in names):
            raise UnsafeBundleError(f"Отсутствует Python runtime: {name}")
    for plugin in QT_PLUGINS:
        if not any(PurePosixPath(item).name.lower() == plugin for item in names):
            raise UnsafeBundleError(f"Отсутствует Qt platform plugin: {plugin}")


def validate_directory(dist: Path) -> set[str]:
    if not dist.is_dir():
        raise UnsafeBundleError(f"Нет каталога сборки: {dist}")
    collected: set[str] = set()
    for path in dist.rglob("*"):
        if path.is_symlink():
            raise UnsafeBundleError(f"Символическая ссылка в сборке: {path}")
        if path.is_file():
            rel = path.relative_to(dist).as_posix()
            _require_safe(PurePosixPath(rel))
            if rel in collected:
                raise UnsafeBundleError(f"Повтор файла в сборке: {rel}")
            collected.add(rel)
    validate_entries(collected)
    return collected


def validate_zip(bundle_zip: Path, expected: set[str] | None = None) -> set[str]:
    if not zipfile.is_zipfile(bundle_zip):
        raise UnsafeBundleError("Архив поставки не является корректным ZIP.")
    collected: set[str] = set()
    with zipfile.ZipFile(bundle_zip) as archive:
        members = archive.infolist()
        for member in members:
            if member.is_dir():
                continue
            name = member.filename.replace("\\", "/")
            parts = PurePosixPath(name).parts
            if not parts or parts[0] != "kontakt":
                raise UnsafeBundleError(f"Неожиданный путь в ZIP: {member.filename}")
            rel = PurePosixPath(*parts[1:])
            _require_safe(rel)
            if str(rel) in collected:
                raise UnsafeBundleError(f"Повтор файла в ZIP: {rel}")
            collected.add(str(rel))
        if archive.testzip() is not None:
            raise UnsafeBundleError("Проверка контрольных сумм ZIP не пройдена.")
    validate_entries(collected)
    if expected is not None and collected != expected:
        raise UnsafeBundleError(
            f"Состав ZIP отличается от onedir: лишние={sorted(collected - expected)}, "
            f"отсутствуют={sorted(expected - collected)}"
        )
    return collected


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--zip", type=Path)
    arguments = parser.parse_args()
    expected = validate_directory(arguments.dist)
    if arguments.zip:
        validate_zip(arguments.zip, expected)
    print(f"Допустимый состав проверен: {len(expected)} файлов.")


if __name__ == "__main__":
    main()
