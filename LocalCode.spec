# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build for the single-file LocalCode.exe.

    .venv\\Scripts\\pyinstaller.exe --clean --noconfirm LocalCode.spec

Bundled: the Flask app, waitress, all server modules and the static UI.
Not bundled (downloaded on demand): llama.cpp runtime, GGUF models, portable Node.
"""
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH)
ICON = ROOT / "assets" / "localcode.ico"

hiddenimports = collect_submodules("server") + [
    "waitress",
    "waitress.server",
    "pypdf",
    "pypdf._reader",
    "pypdf.generic",
]

datas = [
    (str(ROOT / "static"), "static"),
]

a = Analysis(
    [str(ROOT / "app.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter", "unittest", "pydoc", "doctest", "test",
        "PyInstaller", "setuptools", "pip",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="LocalCode",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON) if ICON.exists() else None,
    version=str(ROOT / "assets" / "version_info.txt") if (ROOT / "assets" / "version_info.txt").exists() else None,
)
