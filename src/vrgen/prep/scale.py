"""How big do we make the upload, and what are the prompt's numbers then?

Two facts make this a separate concern from the geometry:

* the canvas geometry is defined by *ratios* (``P`` is a function of the source
  aspect alone), so the padded canvas may be handed to the model at any scale
  without changing what the model is asked to do;
* ``recompose`` restores a returned frame to the manifest's canvas width, so the
  authoritative numbers stay in the manifest -- what we compute here is only what
  the *prompt* should say about the file we actually upload.

So this module answers "what does the uploaded file look like" and nothing else.
It never touches the pixels and never rewrites the manifest.

The band boundaries of a rescaled canvas land on fractional source pixels; that
is harmless (``recompose`` re-derives integer boundaries from the manifest), but
the prompt has to quote *some* integers, so they are rounded to even columns
here and the rounding is reported.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import AigcError
from .budget import InputSpec, even_down, suggest_size

#: Named upload sizes. ``auto`` is the largest size the limits allow (maximum
#: input fidelity); ``half`` is the deliberately cheap one for iterating.
SIZE_MODES = ("auto", "full", "half", "quarter")


@dataclass(frozen=True)
class UploadGeometry:
    """The prompt-visible geometry of the file we hand to the model."""

    canvas_size: tuple[int, int]
    source_size: tuple[int, int]
    band_w: int                      # L' = 2P' (one band's width)
    region_left: tuple[int, int]     # G, half-open column range
    region_main: tuple[int, int]     # R
    region_right: tuple[int, int]    # B
    scale: float                     # upload width / manifest canvas width

    @property
    def P(self) -> int:
        return self.band_w // 2

    def describe(self) -> str:
        return (f"upload {self.canvas_size[0]}x{self.canvas_size[1]} "
                f"({100 * self.scale:.1f}% of canvas)   P={self.P} L={self.band_w}   "
                f"G={self.region_left} R={self.region_main} B={self.region_right}")


def parse_size_mode(text: str) -> tuple[str, tuple[int, int] | None]:
    """``"auto"``/``"half"``/``"4200x1800"`` -> ``(mode, explicit_size_or_None)``."""
    raw = str(text).strip().lower()
    if raw in SIZE_MODES:
        return raw, None
    if "x" in raw:
        left, _, right = raw.partition("x")
        try:
            w, h = int(left), int(right)
        except ValueError as exc:
            raise AigcError(f"--scale {text!r} is not WxH (e.g. 4200x1800)") from exc
        if w < 2 or h < 2 or w % 2 or h % 2:
            raise AigcError(f"--scale {text!r}: both sides must be even and >= 2")
        return "explicit", (w, h)
    raise AigcError(f"unknown scale {text!r}; use one of {', '.join(SIZE_MODES)} or WxH")


def choose_upload_size(canvas_size: tuple[int, int], *, mode: str = "auto",
                       spec: InputSpec | None = None, max_pixels: int = 0,
                       max_bytes: int = 0) -> tuple[int, int]:
    """Pick the upload raster for one geometry + one model's limits.

    ``spec`` describes the *full-size* artifact; omitting it means "the limits are
    unknown", in which case ``auto``/``full`` keep full size and the caller is
    expected to have established that it fits.
    """
    canvas_w, canvas_h = int(canvas_size[0]), int(canvas_size[1])

    if mode == "full":
        return canvas_w, canvas_h
    if mode == "half":
        return even_down(canvas_w / 2, canvas_h / 2)
    if mode == "quarter":
        return even_down(canvas_w / 4, canvas_h / 4)
    if spec is None:
        return canvas_w, canvas_h
    if spec.width == canvas_w and spec.height == canvas_h:
        probe = spec
    else:                                   # spec describes some other raster: rescale it
        probe = InputSpec(width=canvas_w, height=canvas_h, fps=spec.fps,
                          duration_s=spec.duration_s, size_bytes=spec.size_bytes,
                          container=spec.container)
    if max_pixels and max_bytes:
        # byte cap is an area cap for fixed encoder settings, but only if the spec's
        # byte count already belongs to the canvas raster -- otherwise trust pixels.
        if spec.width != canvas_w:
            max_bytes = 0
        return suggest_size(probe, max_pixels, max_bytes)
    return suggest_size(probe, max_pixels, max_bytes)


def plain_geometry(canvas_size: tuple[int, int],
                   upload_size: tuple[int, int]) -> UploadGeometry:
    """Geometry for a non-panorama source: no redundant band, canvas == source.

    ``scaled_geometry`` exists for the fullredraw canvas, where
    ``canvas_w - source_w`` is a real band. gen has no band; using that function
    there would invent a 2-pixel band and print misleading P/L numbers.
    """
    canvas_w = int(canvas_size[0])
    up_w, up_h = int(upload_size[0]), int(upload_size[1])
    if up_w < 4 or up_h < 2:
        raise AigcError(f"upload size {up_w}x{up_h} is too small")
    return UploadGeometry(
        canvas_size=(up_w, up_h),
        source_size=(up_w, up_h),
        band_w=0,
        region_left=(0, 0),
        region_main=(0, up_w),
        region_right=(up_w, up_w),
        scale=up_w / canvas_w,
    )


def scaled_geometry(manifest_canvas: tuple[int, int], manifest_source: tuple[int, int],
                    upload_size: tuple[int, int]) -> UploadGeometry:
    """Proportional geometry of the upload raster, boundaries rounded to even columns."""
    canvas_w, canvas_h = int(manifest_canvas[0]), int(manifest_canvas[1])
    up_w, up_h = int(upload_size[0]), int(upload_size[1])
    if up_w < 4 or up_h < 2:
        raise AigcError(f"upload size {up_w}x{up_h} is too small to be a panorama canvas")

    scale = up_w / canvas_w
    # L = canvas_w - source_w; recompute it from the manifest's own numbers instead
    # of trusting a caller to pass P.
    band_full = canvas_w - int(manifest_source[0])
    band = max(2, int(round(band_full * scale)))
    if band % 2:
        band += 1
    if band >= up_w:
        raise AigcError(f"scaled band {band} does not fit inside a {up_w} px canvas")

    main_w = up_w - band
    return UploadGeometry(
        canvas_size=(up_w, up_h),
        source_size=(main_w, up_h),
        band_w=band,
        region_left=(0, band),
        region_main=(band, main_w),
        region_right=(main_w, up_w),
        scale=scale,
    )
