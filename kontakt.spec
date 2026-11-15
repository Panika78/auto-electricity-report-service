# -*- mode: python ; coding: utf-8 -*-
"""Windows x64 onedir distribution; run only on Windows with PyInstaller."""
from pathlib import Path
import sys

if sys.platform != "win32":
    raise RuntimeError("Сборка kontakt.spec поддерживается только на Windows.")

root = Path(SPECPATH).resolve()
font_dir = root / "app" / "documents" / "fonts"
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

a = Analysis(
    [str(root / "app" / "main.py")],
    pathex=[str(root)],
    binaries=[],
    datas=[(str(font_dir / name), "app/documents/fonts") for name in font_names],
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
