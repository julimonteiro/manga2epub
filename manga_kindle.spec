# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for Manga Kindle — builds a single-file executable
with all dependencies bundled.

Usage:
    pyinstaller manga_kindle.spec
"""

import os
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

# Collect hidden imports that PyInstaller may miss
hidden_imports = [
    "cloudscraper",
    "cloudscraper.interpreters",
    "cloudscraper.interpreters.native",
    "cloudscraper.user_agent",
    "requests",
    "requests.adapters",
    "urllib3",
    "PIL",
    "PIL.Image",
    "PIL.ImageDraw",
    "PIL.ImageFont",
    "bs4",
    "ebooklib",
    "ebooklib.epub",
    "tqdm",
    "rich",
    "rich.console",
    "rich.table",
    "rich.panel",
    "rich.prompt",
    "rich.box",
]

# Collect data files needed at runtime
datas = []

# Collect cloudscraper data files (user agent lists, etc.)
datas += collect_data_files("cloudscraper")

# Collect ebooklib templates
datas += collect_data_files("ebooklib")

a = Analysis(
    ["manga_downloader.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports + collect_submodules("cloudscraper"),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "numpy",
        "scipy",
        "pandas",
        "pytest",
        "setuptools",
        "flask",
        "jinja2",
        "markupsafe",
    ],
    noarchive=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="MangaKindle",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,  # Keep console for terminal UI
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
