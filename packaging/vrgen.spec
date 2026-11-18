# -*- mode: python ; coding: utf-8 -*-
"""vrgen 的 PyInstaller 目录式构建（决策 102）。

产物 ``dist/vrgen/vrgen.exe`` 与 ``_internal/``，安装时整体放到
``<程序目录>\\vrgen\\``。``templates/`` 与 ``config/default.toml`` 进 ``_internal``，
由 ``vrgen.paths`` 按 ``sys._MEIPASS`` 解析；ffmpeg 与 mglc 不在本包里，
由 vrstudio 启动子进程时注入 ``VRGEN_BIN_DIR`` 与 ``VRGEN_MGLC_BIN``。
"""

from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent

datas = [
    (str(ROOT / "templates"), "templates"),
    (str(ROOT / "config" / "default.toml"), "config"),
    (str(ROOT / "src" / "vrgen" / "roles.json"), "vrgen"),
]

a = Analysis(
    [str(ROOT / "packaging" / "entry_vrgen.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=["typer", "click"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="vrgen",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="vrgen",
)
