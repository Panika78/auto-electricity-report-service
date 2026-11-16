# -*- mode: python ; coding: utf-8 -*-
"""Windows x64 onedir distribution; run only on Windows with PyInstaller."""
from pathlib import Path
import sys

if sys.platform != "win32":
    raise RuntimeError("Сборка kontakt.spec поддерживается только на Windows.")

root = Path(SPECPATH).resolve()
font_dir = root / "app" / "documents" / "fonts"
icon_dir = root / "app" / "assets"
icon_file = icon_dir / "kontakt-icon.ico"
icon_svg = icon_dir / "kontakt-icon.svg"
font_names = (
    "PT_Sans-Web-Regular.ttf",
    "PT_Sans-Web-Bold.ttf",
    "PT_Serif-Web-Regular.ttf",
    "PT_Serif-Web-Bold.ttf",
    "OFL-PTSans.txt",
    "OFL-PTSerif.txt",
)
missing = [name for name in font_names if not (font_dir / name).is_file()]
if missing:
    raise RuntimeError("Не найдены шрифты или их лицензии: " + ", ".join(missing))
if not icon_file.is_file() or not icon_svg.is_file():
    raise RuntimeError("Отсутствует оригинальная иконка контакт (.ico/.svg).")

a = Analysis(
    [str(root / "app" / "main.py")],
    pathex=[str(root)],
    binaries=[],
    datas=[(str(font_dir / name), "app/documents/fonts") for name in font_names]
          + [(str(path), "app/assets") for path in (icon_file, icon_svg)],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="kontakt",
    icon=str(icon_file),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="kontakt",
)
