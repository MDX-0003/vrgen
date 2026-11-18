"""资源根的解析：源码检出取仓库根，冻结取 ``sys._MEIPASS``（决策 102）。"""

from __future__ import annotations

import sys
from pathlib import Path

from vrgen import paths


def test_resource_root_in_source_checkout():
    repo = Path(paths.__file__).resolve().parents[2]
    assert paths._resource_root() == repo
    assert (paths.templates_dir() / "360-closure.md").is_file()
    assert (paths.CONFIG_DIR / "default.toml").is_file()


def test_resource_root_frozen_uses_meipass(tmp_path, monkeypatch):
    internal = tmp_path / "_internal"
    internal.mkdir()
    monkeypatch.setattr(sys, "_MEIPASS", str(internal), raising=False)

    assert paths._resource_root() == internal
