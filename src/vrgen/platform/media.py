"""Measure and rescale local media -- the only place this package runs ffmpeg.

Kept apart from ``round/`` because it is a local, free, offline operation; kept out
of ``prep/`` because it is I/O. The rescale exists because the platform's pixel and
byte caps are both below what ``vrpad pad`` produces: the canvas is 4668x2000
(9.34 Mpx against an 8.30 Mpx cap) and, being lossless ``crf 0``, ~228 MB against a
210 MB cap. Both must be fixed before upload, and neither is optional.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .. import media as videoio
from ..errors import AigcError
from ..media import ffmpeg
from ..prep.budget import InputSpec

#: Container reported from the file extension. The platform only checks the
#: container family, and a path suffix is what the CLI itself would infer, so this
#: avoids a second ffprobe call for a field we already know.
_CONTAINER_BY_SUFFIX = {".mp4": "mp4", ".mov": "mov", ".m4v": "mp4", ".webm": "webm"}


def container_of(path: str | Path) -> tuple[str, ...]:
    return (_CONTAINER_BY_SUFFIX.get(Path(path).suffix.lower(), ""),)


def probe_input(path: str | Path, config=None) -> InputSpec:
    """Everything the budget check needs, in one pass."""
    path = Path(path)
    if not path.is_file():
        raise AigcError(f"no such file: {path}")
    info = videoio.probe(path, config)
    duration = info.duration_s
    if duration <= 0 and info.frames and info.fps_float > 0:
        duration = info.frames / info.fps_float
    return InputSpec(
        width=info.size[0],
        height=info.size[1],
        fps=info.fps_float,
        duration_s=duration,
        size_bytes=path.stat().st_size,
        container=container_of(path),
    )


def probe_image(path: str | Path, config=None) -> InputSpec:
    """Image dimensions and bytes via one ffprobe read (no fps/duration)."""
    path = Path(path)
    if not path.is_file():
        raise AigcError(f"no such file: {path}")
    result = subprocess.run(
        [videoio.ffprobe(config), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "json", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise AigcError(
            f"ffprobe could not read image {path.name}: {(result.stderr or '').strip()[:200]}")
    import json
    try:
        streams = (json.loads(result.stdout or "{}").get("streams") or [])
        width = int(streams[0].get("width") or 0)
        height = int(streams[0].get("height") or 0)
    except (IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AigcError(f"ffprobe output for {path.name} is not usable: {exc}") from exc
    if width < 2 or height < 2:
        raise AigcError(f"{path.name}: ffprobe reported no image size")
    suffix = path.suffix.lower().lstrip(".")
    container = ("jpg",) if suffix in ("jpg", "jpeg") else (suffix,)
    return InputSpec(width=width, height=height, fps=0.0, duration_s=0.0,
                     size_bytes=path.stat().st_size, container=container)


def _run(args: list[str], what: str) -> None:
    result = subprocess.run(args, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode != 0:
        tail = (result.stderr or "").strip()[:400]
        raise AigcError(f"{what} failed (ffmpeg exit {result.returncode}):\n{tail}")


def scale_video(src: str | Path, dst: str | Path, size: tuple[int, int], *,
                config=None, crf: int = 16, keep_audio: bool = True,
                log=None) -> Path:
    """Rescale to ``size`` for upload. Frame rate and duration are left alone.

    ``crf 16`` rather than a lossless profile on purpose: the model's own output
    tops out at the 1080p class, so detail beyond that is not recoverable anyway,
    while the byte cap is a hard gate.
    """
    src, dst = Path(src), Path(dst)
    if size[0] % 2 or size[1] % 2:
        raise AigcError(f"upload size {size[0]}x{size[1]} must be even (yuv420p)")
    dst.parent.mkdir(parents=True, exist_ok=True)
    args = [ffmpeg(config), "-y", "-v", "error", "-i", str(src),
            "-vf", f"scale={size[0]}:{size[1]}",
            "-c:v", "libx264", "-crf", str(int(crf)), "-preset", "veryfast",
            "-pix_fmt", "yuv420p"]
    args += ["-c:a", "copy"] if keep_audio else ["-an"]
    args.append(str(dst))
    if log is not None:
        log.info(f"scaling {src.name} -> {size[0]}x{size[1]}  {dst.name}")
    _run(args, f"scaling {src.name}")
    return dst


def scale_image(src: str | Path, dst: str | Path, size: tuple[int, int], *,
                config=None) -> Path:
    """Nearest-neighbour rescale, for the binary region mask.

    Nearest neighbour, not a smooth filter: the mask is a geometric record and
    interpolating it would invent a grey border that says "partially redundant",
    which is not a thing.
    """
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    _run([ffmpeg(config), "-y", "-v", "error", "-i", str(src),
          "-vf", f"scale={size[0]}:{size[1]}:flags=neighbor",
          "-c:v", "png", "-pix_fmt", "gray", str(dst)],
         f"rescaling {src.name}")
    return dst


def scale_reference_image(src: str | Path, dst: str | Path, size: tuple[int, int], *,
                          config=None) -> Path:
    """Smooth rescale for a reference image; output PNG keeps the pixels lossless."""
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    _run([ffmpeg(config), "-y", "-v", "error", "-i", str(src),
          "-vf", f"scale={size[0]}:{size[1]}:flags=lanczos",
          "-frames:v", "1", str(dst)],
         f"rescaling image {src.name}")
    return dst
