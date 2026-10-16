"""路径常量与小的哈希工具（自包含，不依赖任何兄弟仓库）。"""

from __future__ import annotations

import hashlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
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
