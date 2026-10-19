"""The generation page's two resolution choices.

There are two different numbers behind "resolution", and mixing them up is the
mistake this module exists to prevent:

* **output resolution** is the platform's ``--resolution`` tier (480p/720p/
  1080p). It is what the model returns (21:9 is 1470x630 at 720p, measured on
  ship_01; the other rows come from the platform's published pixel table).
* **input resolution** is how large the file we upload is. The model refuses
  rasters outside its pixel/byte budget, so vrgen rescales before upload. The
  prompt's ``{canvas_w}x{canvas_h}`` and band columns describe *this* file, not
  the output.

Only three input modes are offered to callers: ``auto`` (largest size that fits
the model's limits), ``full`` (the manifest canvas), ``half`` (half the canvas).
The internal scale parser still understands explicit WxH for debugging, but it
is deliberately absent from the UI and from ``generation.toml``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from .budget import InputSpec, check_input, even_down
from .scale import choose_upload_size

INPUT_MODES = ("auto", "full", "half")
OUTPUT_TIERS = ("480p", "720p", "1080p")

#: 面向视频素材的生成模式；label 只用于页面展示，值仍然是 mglc 的模式名。
MODE_LABELS = {
    "videoEdit": "VideoEdit · 结构锁定编辑（动作跟随输入）",
    "allMediaRef": "AllMediaRef · 多素材参考（可重新生成动作）",
    "videoExtend": "VideoExtend · 延长视频",
    "firstFrame": "FirstFrame · 首帧生视频",
    "firstLastFrame": "FirstLastFrame · 首尾帧生视频",
}

#: 21:9 outputs. 720p is the measured ship_01 result; 480p/1080p are the
#: platform's published table. The gate in run.json is the final authority for
#: whatever the platform actually returned.
OUTPUT_SIZES_21_9: dict[str, tuple[int, int]] = {
    "480p": (992, 432),
    "720p": (1470, 630),
    "1080p": (2206, 946),
}

#: Canonical 16:9 tiers, used for the patch viewport (and as a fallback label
#: for unusual aspects). 21:9 has its own table because the platform does not
#: simply keep the short side fixed.
OUTPUT_SIZES_16_9: dict[str, tuple[int, int]] = {
    "480p": (854, 480),
    "720p": (1280, 720),
    "1080p": (1920, 1080),
}


def output_options(aspect: float) -> list[dict]:
    """Output tier -> nominal raster for this canvas aspect."""
    if abs(aspect - 21 / 9) < 0.02:
        table = OUTPUT_SIZES_21_9
    elif abs(aspect - 16 / 9) < 0.02:
        table = OUTPUT_SIZES_16_9
    else:
        table = OUTPUT_SIZES_16_9
    return [{"value": name, "width": table[name][0], "height": table[name][1]}
            for name in OUTPUT_TIERS]


def input_options(canvas_size: tuple[int, int], spec: InputSpec,
                  validator: Mapping[str, object]) -> list[dict]:
    """Input scale -> actual upload raster + whether it clears the limits.

    ``auto`` is computed with the same function the submit path uses, so the
    number shown in the browser is the number that will be uploaded. ``full``
    and ``half`` are pure canvas arithmetic; the byte estimate for a rescaled
    option uses area-proportional scaling, which is the same heuristic as
    ``budget.suggest_size`` (the real file is re-encoded at upload_crf).
    """
    canvas_w, canvas_h = int(canvas_size[0]), int(canvas_size[1])
    max_pixels = int(validator.get("maxPixel") or 0)
    max_bytes = int(validator.get("maxBytes") or 0)
    auto = choose_upload_size(canvas_size, mode="auto", spec=spec,
                              max_pixels=max_pixels, max_bytes=max_bytes)
    sizes = {
        "auto": auto,
        "full": (canvas_w, canvas_h),
        "half": even_down(canvas_w / 2, canvas_h / 2),
    }
    out: list[dict] = []
    for name in INPUT_MODES:
        width, height = sizes[name]
        if name == "full":
            candidate = spec
        else:
            ratio = (width * height) / max(spec.pixels, 1)
            candidate = InputSpec(width=width, height=height, fps=spec.fps,
                                  duration_s=spec.duration_s,
                                  size_bytes=int(spec.size_bytes * ratio),
                                  container=spec.container)
        report = check_input(candidate, validator)
        out.append({
            "value": name,
            "width": width,
            "height": height,
            "ok": report.ok,
            "reason": "; ".join(c.line() for c in report.failures),
        })
    return out


def derived_duration(spec: InputSpec) -> int:
    """The integer seconds submit sends: ``videoEdit`` follows the input timeline."""
    seconds = spec.duration_s if spec.duration_s > 0 else 0.0
    return max(4, int(math.ceil(seconds)))


def effective_aspect(aspect_ratio: str, ratios: Sequence[str]) -> str:
    """Resolve the aspect value sent to mglc for this mode.

    Modes with an empty ratio list (``videoEdit`` / ``videoExtend``) follow the
    input; ``auto`` then means "do not send the flag". Modes such as
    ``allMediaRef`` publish a closed ratio list, so ``auto`` maps to 16:9 when
    available, otherwise the first published ratio.
    """
    value = str(aspect_ratio or "").strip()
    if not ratios:
        return "auto" if value in ("", "auto") else value
    if value in ratios:
        return value
    if value in ("", "auto"):
        return "16:9" if "16:9" in ratios else ratios[0]
    return value
