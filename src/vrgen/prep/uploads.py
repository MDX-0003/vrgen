"""Plan and produce the upload copies of the media a round would send.

The platform's per-model limits decide what the upload raster may be, and the
re-encode is the same work whether or not a submission follows. ``submit`` needs
both halves in the middle of its local half; ``vrgen prep`` needs them on their
own. This module is that shared middle: nothing here touches a run directory,
the ledger or the network -- callers say where the files go.

The rules themselves stay in ``prep.scale`` / ``prep.budget``; this module only
orders them the way a round does and keeps the error wording of ``pipeline``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from ..errors import BudgetError
from ..platform.media import (
    probe_image,
    probe_input,
    scale_image,
    scale_reference_image,
    scale_video,
)
from .budget import InputSpec, check_image, check_input, fit_image_size
from .scale import choose_upload_size


@dataclass(frozen=True)
class UploadPlan:
    """What one asset should become on the wire.

    ``size`` is the raster the upload will have; ``needs_scale`` says whether the
    source already *is* that raster (no re-encode needed).
    """

    asset_id: str
    kind: str
    role: str
    src: Path
    spec: InputSpec
    size: tuple[int, int]
    needs_scale: bool


def _budget_error(asset_id: str, report, *, suffix: str = "") -> BudgetError:
    lines = "\n".join("  " + c.line() for c in report.failures)
    return BudgetError(f"素材 {asset_id} {suffix}：\n{lines}")


def plan_video(asset_id: str, path: Path, *, conf, validator: Mapping[str, object],
               mode_name: str,
               explicit: tuple[int, int] | None = None) -> UploadPlan:
    """A video asset: pass-through when it fits, otherwise the largest size that does."""
    src = Path(path)
    spec = probe_input(src, conf)
    report = check_input(spec, validator)
    labels = {c.label for c in report.failures}
    if labels - {"pixels", "size"}:
        raise _budget_error(asset_id, report, suffix="不合约")
    size = (spec.width, spec.height)
    if report.failures:
        size = explicit or choose_upload_size(
            (spec.width, spec.height), mode=mode_name, spec=spec,
            max_pixels=int(validator.get("maxPixel") or 0),
            max_bytes=int(validator.get("maxBytes") or 0))
        if size == (spec.width, spec.height):
            raise BudgetError(
                f"素材 {asset_id} 在当前 --scale={mode_name} 下仍不合约：\n"
                + "\n".join("  " + c.line() for c in report.failures))
    return UploadPlan(asset_id=asset_id, kind="video", role="edit_master",
                      src=src, spec=spec, size=size,
                      needs_scale=size != (spec.width, spec.height))


def plan_image(asset_id: str, path: Path, *, conf, validator: Mapping[str, object],
               kind: str = "image", role: str = "reference") -> UploadPlan:
    """A reference image: fit the image caps, keeping the aspect ratio."""
    src = Path(path)
    spec = probe_image(src, conf)
    report = check_image(spec, validator)
    labels = {c.label for c in report.failures}
    if labels - {"pixels", "size", "width", "height"}:
        raise _budget_error(asset_id, report, suffix="不合约")
    size = (spec.width, spec.height)
    if report.failures:
        size = fit_image_size(spec, validator)
        if size == (spec.width, spec.height):
            raise BudgetError(
                f"素材 {asset_id} 无法通过缩放满足图片限制：\n"
                + "\n".join("  " + c.line() for c in report.failures))
    return UploadPlan(asset_id=asset_id, kind=kind, role=role,
                      src=src, spec=spec, size=size,
                      needs_scale=size != (spec.width, spec.height))


def plan_mask(asset_id: str, path: Path, *, conf, canvas_size: tuple[int, int],
              validator: Mapping[str, object]) -> UploadPlan:
    """The edit mask: always the master's upload canvas, never the image budget."""
    src = Path(path)
    spec = probe_image(src, conf)
    size = (int(canvas_size[0]), int(canvas_size[1]))
    # Size lands after the re-encode, so only sides/pixels/aspect are checked here.
    report = check_image(InputSpec(width=size[0], height=size[1], fps=0.0,
                                   duration_s=0.0, size_bytes=0,
                                   container=spec.container), validator)
    if not report.ok:
        raise BudgetError(
            f"mask {asset_id} 缩放到母版上传画布 {size[0]}x{size[1]} 后仍不合约：\n"
            + "\n".join("  " + c.line() for c in report.failures))
    return UploadPlan(asset_id=asset_id, kind="image", role="mask",
                      src=src, spec=spec, size=size,
                      needs_scale=size != (spec.width, spec.height))


def materialize(plan: UploadPlan, out_dir: Path, *, conf, log=None,
                image_validator: Mapping[str, object] | None = None,
                video_validator: Mapping[str, object] | None = None
                ) -> tuple[Path, int, int]:
    """Write the upload copy into ``out_dir``; returns ``(path, width, height)``.

    An asset that already fits is returned untouched (the caller's upload is the
    source file itself). Scaled copies are named ``upload_<id>_<W>x<H>`` and are
    re-probed so the result, not the intent, is what gets reported.
    """
    if not plan.needs_scale:
        return plan.src, plan.spec.width, plan.spec.height
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    width, height = plan.size
    stem = out_dir / f"upload_{plan.asset_id}_{width}x{height}"
    if plan.role == "mask":
        upload = scale_image(plan.src, stem.with_suffix(".png"), plan.size, config=conf)
        measured = probe_image(upload, conf)
        recheck = check_image(measured, image_validator or {})
    elif plan.kind == "video":
        upload = scale_video(plan.src, stem.with_suffix(".mp4"), plan.size,
                             config=conf, crf=int(conf.get("mglc.upload_crf", 16)),
                             log=log)
        measured = probe_input(upload, conf)
        recheck = check_input(measured, video_validator or {})
    else:
        upload = scale_reference_image(plan.src, stem.with_suffix(".png"),
                                       plan.size, config=conf)
        measured = probe_image(upload, conf)
        recheck = check_image(measured, image_validator or {})
    if not recheck.ok:
        raise BudgetError(
            f"素材 {plan.asset_id} 缩放后仍不合约：\n"
            + "\n".join("  " + c.line() for c in recheck.failures))
    return Path(upload), measured.width, measured.height
