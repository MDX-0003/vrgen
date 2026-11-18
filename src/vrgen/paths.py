"""路径常量与小的哈希工具（自包含，不依赖任何兄弟仓库）。

冻结（PyInstaller 目录式）后代码在 ``sys._MEIPASS``（即 ``_internal``）里，
``templates/`` 与 ``config/`` 随包放在那里，所以资源根优先取 ``_MEIPASS``；
源码检出时它等于仓库根。可执行二进制（ffmpeg、mglc）不在这里找：vrstudio 启动
本 CLI 时注入 ``VRGEN_BIN_DIR`` 与 ``VRGEN_MGLC_BIN``（决策 102）。
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path


def _resource_root() -> Path:
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if base else Path(__file__).resolve().parents[2]


REPO_ROOT = _resource_root()
TEMPLATES_DIR = REPO_ROOT / "templates"
CONFIG_DIR = REPO_ROOT / "config"


def repo_root() -> Path:
    return REPO_ROOT


def templates_dir() -> Path:
    return TEMPLATES_DIR


def sha256_16(path: str | Path) -> str:
    """文件 sha256 的前 16 位——run_key 的输入指纹（沿 vrpad manifest 同名语义）。"""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]
