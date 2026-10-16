"""Does this file fit the model's published input limits?  Pure arithmetic.

Every number here comes from the platform's own public model list (see
``platform.models``) -- nothing is hard-coded, because a hard-coded limit that
drifts from the platform silently produces a rejected submission, and a rejected
submission still costs the round trip (a generation task cannot be cancelled).

Two independent budgets have to be satisfied, and they fail differently:

* **pixels** -- the model refuses to look at a raster that is too large;
* **bytes**  -- the upload is refused outright. This one is easy to forget:
  ``vrpad pad`` writes lossless ``crf 0``, and a lossless 4668x2000 8-second file
  is ~228 MB against a 210 MB cap. The pixel budget and the byte budget are not
  proportional, so both must be checked.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ..errors import BudgetError


@dataclass(frozen=True)
class InputSpec:
    """What we measured about a candidate upload file."""

    width: int
    height: int
    fps: float
    duration_s: float
    size_bytes: int
    container: tuple[str, ...] = ()

    @property
    def pixels(self) -> int:
        return self.width * self.height

    @property
    def aspect(self) -> float:
        return self.width / self.height if self.height else 0.0

    def describe(self) -> str:
        return (f"{self.width}x{self.height} = {self.pixels:,} px   {self.fps:.3f} fps   "
                f"{self.duration_s:.3f}s   {self.size_bytes / 1e6:.1f} MB   "
                f"aspect {self.aspect:.4f}")


@dataclass(frozen=True)
class Check:
    label: str
    ok: bool
    detail: str

    def line(self) -> str:
        return f"{'PASS' if self.ok else 'FAIL'}  {self.label:10} {self.detail}"


@dataclass(frozen=True)
class BudgetReport:
    spec: InputSpec
    checks: tuple[Check, ...]
    limits: Mapping[str, object]

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    @property
    def failures(self) -> tuple[Check, ...]:
        return tuple(c for c in self.checks if not c.ok)

    def suggested_size(self) -> tuple[int, int] | None:
        """Largest even-aligned size that satisfies both the pixel and byte caps."""
        if int(self.limits.get("maxPixel") or 0) <= 0:
            return None
        return suggest_size(self.spec, int(self.limits["maxPixel"]),
                            int(self.limits.get("maxBytes") or 0))

    def raise_if_bad(self) -> None:
        if self.ok:
            return
        lines = "\n".join("  " + c.line() for c in self.failures)
        hint = self.suggested_size()
        tail = f"\n  suggested resize: {hint[0]}x{hint[1]}" if hint else ""
        raise BudgetError(f"input does not fit the model's limits:\n{lines}{tail}")


def even_down(width: float, height: float, minimum: int = 2) -> tuple[int, int]:
    """Floor both sides to even integers (yuv420p needs even dimensions)."""
    w = max(minimum, int(width) - int(width) % 2)
    h = max(minimum, int(height) - int(height) % 2)
    return w, h


def suggest_size(spec: InputSpec, max_pixels: int, max_bytes: int = 0,
                 min_pixels: int = 0) -> tuple[int, int]:
    """Scale ``spec`` down to the largest size that clears both caps.

    The byte cap scales with area for a fixed encoder setting, and the pixel cap
    is an area cap, so a single linear factor ``sqrt(cap/current)`` serves both;
    the tighter of the two wins.
    """
    factors = []
    if max_pixels and spec.pixels > max_pixels:
        factors.append((max_pixels / spec.pixels) ** 0.5)
    if max_bytes and spec.size_bytes > max_bytes:
        factors.append((max_bytes / spec.size_bytes) ** 0.5)
    factor = min(factors) if factors else 1.0

    width, height = even_down(spec.width * factor, spec.height * factor)
    if min_pixels and width * height < min_pixels:
        raise BudgetError(
            f"cannot satisfy both caps at this aspect: {max_pixels:,} px budget vs "
            f"{min_pixels:,} px minimum leaves no room for {spec.width}x{spec.height}"
        )
    return width, height


def check_input(spec: InputSpec, validator: Mapping[str, object]) -> BudgetReport:
    """Compare ``spec`` against one ``refVideoValidator`` block from the model list."""
    checks: list[Check] = []

    min_px = int(validator.get("minPixel") or 0)
    max_px = int(validator.get("maxPixel") or 0)
    if max_px or min_px:
        checks.append(Check("pixels", min_px <= spec.pixels <= max_px,
                            f"{spec.pixels:,} px, budget {min_px:,}..{max_px:,}"))

    fps = validator.get("fps") or {}
    fps_lo, fps_hi = float(fps.get("min") or 0), float(fps.get("max") or 1e9)
    checks.append(Check("fps", fps_lo <= spec.fps <= fps_hi,
                        f"{spec.fps:.3f}, allowed {fps_lo:g}..{fps_hi:g}"))

    duration = validator.get("duration") or {}
    dur_lo = float(duration.get("min") or 0)
    dur_hi = float(duration.get("max") or 1e9)
    checks.append(Check("duration", dur_lo <= spec.duration_s <= dur_hi,
                        f"{spec.duration_s:.3f}s, allowed {dur_lo:g}..{dur_hi:g}s"))

    formats = tuple(str(f) for f in (validator.get("formats") or ()))
    if formats:
        hit = any(f in spec.container for f in formats)
        checks.append(Check("container", hit, f"{list(spec.container)} vs {list(formats)}"))

    max_bytes = int(validator.get("maxBytes") or 0)
    if max_bytes:
        checks.append(Check("size", spec.size_bytes <= max_bytes,
                            f"{spec.size_bytes / 1e6:.1f} MB, max {max_bytes / 1e6:.0f} MB"))

    lo = float(validator.get("refVideoMinAspectRatio") or 0.0)
    hi = float(validator.get("refVideoMaxAspectRatio") or 0.0) or 1e9
    if lo or hi < 1e9:
        checks.append(Check("aspect", lo <= spec.aspect <= hi,
                            f"{spec.aspect:.4f}, allowed {lo:g}..{hi:g}"))

    return BudgetReport(spec=spec, checks=tuple(checks), limits=validator)
