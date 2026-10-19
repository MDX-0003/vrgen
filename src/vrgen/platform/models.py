"""What the platform says each model can do -- fetched, not assumed.

The numbers that decide whether a submission is even accepted (pixel budget, byte
cap, fps window, duration range, which modes take a video) live on a public,
unauthenticated endpoint. Fetching them costs nothing, so nothing here is
hard-coded: a limit baked into this file would go stale silently and produce a
rejected submission -- which costs a round trip *and* is not cancellable.

What is verified today (2026-09-23, ``GET /api/v1/aitools/videoModelList``):

* 23 video models; Seedance at codes 37 (2.5), 22 (2.0), 23 (2.0 Fast), 14 (1.5 Pro);
* only **2.5 has ``videoEdit``** -- a structurally locked edit, where the input video
  becomes a span of the output timeline. 2.0/2.0 Fast accept a video only through
  ``allMediaRef``, which is a *semantic* reference: the output does not preserve the
  input's frame-by-frame structure, so it cannot be used for a redundant-band task;
* Seedance reads reference media only together with an SD-compliance asset id
  (``supportVirtualIp``), so ``round.assets`` is mandatory on that path.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import AigcError

MODEL_LIST_URL = "https://aigc.mgtv.com/api/v1/aitools/videoModelList"
CACHE_NAME = "models.json"


@dataclass(frozen=True)
class ModeLimits:
    """One ``referenceModes`` entry: what a generation mode accepts."""

    mode: str
    support_virtual_ip: bool
    max_count: int
    resolutions: tuple[str, ...] = ()
    default_resolution: str = ""
    duration: Mapping[str, Any] = None            # type: ignore[assignment]
    ratios: tuple[str, ...] = ()
    ref_video: Mapping[str, Any] | None = None
    ref_image: Mapping[str, Any] | None = None
    ref_audio: Mapping[str, Any] | None = None

    @property
    def takes_video(self) -> bool:
        return self.ref_video is not None

    def ref_video_limits(self) -> Mapping[str, Any]:
        if self.ref_video is None:
            raise AigcError(f"mode {self.mode!r} does not accept an input video")
        return self.ref_video

    def describe(self) -> str:
        dur = self.duration or {}
        span = (f"{dur.get('min')}-{dur.get('max')}s"
                if dur.get("type") == "range" else "locked to the input")
        return (f"{self.mode}: video={'yes' if self.takes_video else 'no'}"
                f" (max {self.max_count})  asset_id={'required' if self.support_virtual_ip else 'not used'}"
                f"  {span}  {list(self.resolutions)}")


@dataclass(frozen=True)
class ModelInfo:
    code: int
    name: str
    description: str
    max_prompt_len: int | None
    modes: Mapping[str, ModeLimits]
    raw: Mapping[str, Any] = None                 # type: ignore[assignment]

    @property
    def video_input_modes(self) -> tuple[str, ...]:
        """Modes that accept an input video, in the platform's own order."""
        return tuple(name for name, m in self.modes.items() if m.takes_video)

    @property
    def estimated_seconds(self) -> int | None:
        """The platform's own recent median render time, when it reports one.

        Worth printing before a wait: without it, several minutes of polling is
        indistinguishable from a hang.
        """
        value = (self.raw or {}).get("estimatedDurationSeconds")
        try:
            return int(value) if value else None
        except (TypeError, ValueError):
            return None

    def mode(self, name: str) -> ModeLimits:
        if name not in self.modes:
            raise AigcError(
                f"model {self.code} ({self.name}) has no mode {name!r}; "
                f"available: {', '.join(self.modes) or '(none)'}"
                + (f"; modes taking a video: {', '.join(self.video_input_modes) or '(none)'}"
                   if name else "")
            )
        return self.modes[name]

    def describe(self) -> str:
        return (f"code {self.code}  {self.name}  (prompt <= {self.max_prompt_len or '?'} chars)\n"
                + "\n".join(f"    {m.describe()}" for m in self.modes.values()))


def parse_models(items: Iterable[Mapping[str, Any]]) -> list[ModelInfo]:
    out: list[ModelInfo] = []
    for raw in items:
        if not isinstance(raw, Mapping) or "code" not in raw:
            continue
        cfg = raw.get("modelConfig") or {}
        modes: dict[str, ModeLimits] = {}
        for entry in cfg.get("referenceModes") or ():
            mode = str(entry.get("mode") or "")
            if not mode:
                continue
            modes[mode] = ModeLimits(
                mode=mode,
                support_virtual_ip=bool(entry.get("supportVirtualIp")),
                max_count=int(entry.get("maxCount") or 0),
                resolutions=tuple(str(r) for r in (entry.get("resolutions") or ())),
                default_resolution=str(entry.get("defaultResolution") or ""),
                duration=entry.get("duration") or {},
                ratios=tuple(str(r) for r in (entry.get("ratios") or ())),
                ref_video=entry.get("refVideoValidator"),
                ref_image=entry.get("refImageValidator"),
                ref_audio=entry.get("refAudioValidator"),
            )
        text2video = cfg.get("text2VideoMode")
        if text2video and "text2video" not in modes:
            modes["text2video"] = ModeLimits(
                mode="text2video",
                support_virtual_ip=False,
                max_count=int(cfg.get("defaultGenerateCount") or 1),
                resolutions=tuple(str(r) for r in (text2video.get("resolutions") or ())),
                default_resolution=str(text2video.get("defaultResolution") or ""),
                duration=text2video.get("duration") or {},
                ratios=tuple(str(r) for r in (text2video.get("ratios") or ())),
            )
        out.append(ModelInfo(
            code=int(raw["code"]),
            name=str(raw.get("displayName") or raw.get("name") or ""),
            description=str(raw.get("description") or ""),
            max_prompt_len=(int(cfg["maxPromptLength"]) if cfg.get("maxPromptLength")
                            else None),
            modes=modes,
            raw=raw,
        ))
    return out


def find_model(models: Sequence[ModelInfo], code: int) -> ModelInfo:
    matches = [m for m in models if m.code == int(code)]
    if not matches:
        raise AigcError(
            f"model id {code} is not in the platform's model list "
            f"(have: {', '.join(str(m.code) for m in models)}).\n"
            f"  set aigc.model_id in config/; code 37 is Seedance 2.5, the only model with "
            f"the structurally locked `videoEdit` mode this tool needs"
        )
    # The platform lists some models twice: a cheaper "sample" entry and the full
    # one under the same code (37 is the current example; the sample entry only
    # advertises 480p). Pick the entry with the most capabilities so resolution
    # tiers and limits come from the full model, not the sample UI variant.
    return max(matches, key=lambda m: (
        len(m.modes), sum(len(mode.resolutions) for mode in m.modes.values())))


def fetch_items(url: str = MODEL_LIST_URL, timeout: float = 60.0) -> list[dict]:
    """One unauthenticated GET. Costs no quota."""
    request = urllib.request.Request(url, headers={"User-Agent": "vrpad-aigc"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
        raise AigcError(f"could not fetch the model list from {url}: {exc}") from exc
    data = payload.get("data")
    items = data.get("items") if isinstance(data, Mapping) else None
    if not isinstance(items, list):
        raise AigcError(f"{url} answered in an unexpected shape: {list(payload)}")
    return items


def cache_path(workdir: str | Path) -> Path:
    return Path(workdir) / CACHE_NAME


def load_models(workdir: str | Path, *, refresh: bool = False,
                max_age_h: float = 24.0, url: str = MODEL_LIST_URL,
                fetcher: Callable[[str, float], list[dict]] = fetch_items,
                log=None) -> list[ModelInfo]:
    """Cached model list, refreshed when stale or asked for.

    A stale cache beats a hard failure when the network is down: the limits are
    stable enough that yesterday's copy is still worth checking against, and the
    caller is told it is stale.
    """
    path = cache_path(workdir)
    cached: list[dict] | None = None
    age_h = None
    if path.is_file():
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
            cached = blob.get("items")
            age_h = (time.time() - float(blob.get("fetched_at", 0))) / 3600.0
        except (json.JSONDecodeError, TypeError, ValueError, OSError):
            cached = None

    fresh = cached is not None and age_h is not None and age_h <= max_age_h
    if fresh and not refresh:
        return parse_models(cached or [])

    try:
        items = fetcher(url, 60.0)
    except AigcError as exc:
        if cached:
            if log is not None:
                log.warn(f"model list fetch failed ({exc}); using the cache from "
                         f"{age_h:.1f}h ago")
            return parse_models(cached)
        raise

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"fetched_at": time.time(), "url": url, "items": items},
                               ensure_ascii=False), encoding="utf-8")
    if log is not None:
        log.info(f"model list: {len(items)} entries from {url}")
    return parse_models(items)
