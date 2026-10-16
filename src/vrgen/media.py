"""ffmpeg / ffprobe 的发现与探测——vrgen 自用（不依赖 vrstudio 的 media 包）。

发现顺序：``$VRGEN_BIN_DIR`` > ``<repo>/bin`` > ``PATH``。
vrgen 不随仓库分发二进制，所以 PATH 回退无条件开启（与 vrstudio 的分发原则不同，
那里 bin/ 必须命中；这里 mglc/ffmpeg 都是机器级工具）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .errors import VrgenError
from .paths import REPO_ROOT


def _find(name: str) -> str:
    override = os.environ.get("VRGEN_BIN_DIR")
    if override:
        candidate = Path(override) / name
        if candidate.is_file():
            return str(candidate)
    bundled = REPO_ROOT / "bin" / name
    if bundled.is_file():
        return str(bundled)
    found = shutil.which(name)
    if found:
        return found
    raise VrgenError(f"找不到 {name}（找过 $VRGEN_BIN_DIR、{REPO_ROOT / 'bin'}、PATH）")


def ffmpeg(config=None) -> str:
    return _find(str(config.get("mglc.ffmpeg", "ffmpeg.exe") if config else "ffmpeg.exe")
                 if os.name == "nt" else "ffmpeg")


def ffprobe(config=None) -> str:
    return _find("ffprobe.exe" if os.name == "nt" else "ffprobe")


@dataclass(frozen=True)
class ProbeInfo:
    size: tuple[int, int]
    fps_float: float
    frames: int
    duration_s: float
    has_audio: bool


def probe(path: str | Path, config=None) -> ProbeInfo:
    """一次 ffprobe 拿全平台侧预算检查需要的字段。"""
    path = Path(path)
    r = subprocess.run(
        [ffprobe(config), "-v", "error",
         "-select_streams", "v:0",
         "-show_entries", "stream=width,height,avg_frame_rate,nb_frames,duration",
         "-show_entries", "format=duration",
         "-of", "json", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise VrgenError(f"ffprobe 失败（{path.name}）：{(r.stderr or '').strip()[:200]}")
    try:
        data = json.loads(r.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise VrgenError(f"ffprobe 输出不是 JSON：{exc}") from exc
    streams = data.get("streams") or []
    if not streams:
        raise VrgenError(f"{path} 没有视频流")
    s = streams[0]
    w, h = int(s.get("width") or 0), int(s.get("height") or 0)
    if not w or not h:
        raise VrgenError(f"{path}：ffprobe 没报视频尺寸")
    fps = 0.0
    rate = str(s.get("avg_frame_rate") or "")
    if "/" in rate:
        num, _, den = rate.partition("/")
        if den and float(den):
            fps = float(num) / float(den)
    elif rate:
        fps = float(rate)
    duration = 0.0
    for raw in (s.get("duration"), data.get("format", {}).get("duration")):
        try:
            if raw:
                duration = float(raw)
                break
        except (TypeError, ValueError):
            pass
    frames = 0
    try:
        frames = int(s.get("nb_frames") or 0)
    except (TypeError, ValueError):
        frames = 0
    audio = subprocess.run(
        [ffprobe(config), "-v", "error", "-select_streams", "a", "-show_entries",
         "stream=index", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True)
    return ProbeInfo(size=(w, h), fps_float=fps, frames=frames, duration_s=duration,
                     has_audio=bool((audio.stdout or "").strip()))
