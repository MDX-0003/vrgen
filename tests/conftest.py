"""共享 fixtures。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def util_video(tmp_path_factory) -> Path:
    """一个 3 帧的真视频（有 ffmpeg 时）；造不出就退化为假字节文件——
    两种情况下 gate 分别走 ok 与 skipped 分支，测试断言两者皆可。"""
    path = tmp_path_factory.mktemp("media") / "tiny.mp4"
    ff = shutil.which("ffmpeg")
    if ff:
        r = subprocess.run(
            [ff, "-y", "-v", "error", "-f", "lavfi", "-i",
             "color=c=blue:s=64x48:d=0.1", "-r", "30", "-pix_fmt", "yuv420p", str(path)],
            capture_output=True,
        )
        if r.returncode == 0 and path.is_file():
            return path
    path.write_bytes(b"not a real video, gate must be skipped")
    return path
