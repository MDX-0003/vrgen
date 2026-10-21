"""The SD-compliance asset library (``virtual-ip``) -- mandatory for Seedance.

The CLI help says it three times, once per media kind:

    参考图片的SD合规素材ID，可重复，当模型为seedance系列时，参考图片必须与SD合规素材ID对应。

and the platform's own model list agrees (``supportVirtualIp: true`` on the modes we
use). So the padded video and the mask cannot simply be handed to the model as
paths: each has to be registered as an asset and pass an **asynchronous review**
before a generation request will accept it.

Two consequences shape this module:

* uploading is decoupled from generating, so the review wait happens once and the
  asset id is cached in the run ledger -- regenerating a round does not re-upload;
* the review is asynchronous and the CLI has no blocking flag, so we poll.

**The response shapes below are not verified.** No authenticated call has been made
yet, so ``extract_asset_id`` and ``status_of`` search the payload for the plausible
keys instead of indexing fixed paths, and raise with the raw JSON when they find
nothing. The first real run against the service is what pins them down.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from ..errors import AssetError, MglcError
from ..platform.client import Envelope, Mglc, walk_scalars
from .ledger import Run

#: Kinds the CLI accepts, mapped to whether the input is an image or a video.
KIND_VIDEO = "video"
KIND_IMAGE = "image"

ACTIVE_STATES = {"active", "approved", "passed", "success", "succeeded", "ready", "done"}
FAILED_STATES = {"failed", "rejected", "reject", "error", "invalid", "blocked"}

_ASSET_ID_KEYS = ("assetid", "portraitassetid", "materialid", "mediaid", "fileid", "id")
_STATUS_KEYS = ("auditstatus", "reviewstatus", "status", "state", "auditstate")


def extract_asset_id(data: Any) -> int:
    """Pull the asset id out of an upload/info payload, whatever it is nested in."""
    candidates: list[tuple[int, str, Any]] = []
    for key, value in walk_scalars(data):
        low = key.lower()
        for rank, wanted in enumerate(_ASSET_ID_KEYS):
            if low == wanted or low.endswith(wanted):
                try:
                    candidates.append((rank, key, int(value)))
                except (TypeError, ValueError):
                    pass
                break
    if not candidates:
        raise AssetError(
            "could not find an asset id in the response. This shape is unverified "
            "(the first authenticated run should pin it down); raw payload:\n"
            f"  {data!r}"
        )
    candidates.sort(key=lambda item: (item[0],))
    return candidates[0][2]


def status_of(data: Any) -> str:
    """Best-effort review status string; ``""`` when the payload has none yet."""
    for key, value in walk_scalars(data):
        if key.lower() in _STATUS_KEYS and isinstance(value, str):
            return value.strip().lower()
    return ""


def classify(status: str) -> str:
    """``"active"`` | ``"pending"`` | ``"failed"`` | ``"unknown"``.

    An empty status is ``"unknown"``, **not** ``"pending"``. That mapping is what
    turned a dead asset id into an endless "waiting for review" loop: the service
    answers ``code 0`` with a payload that says nothing about review state, and
    treating silence as "still being reviewed" waits for a verdict that never comes.
    """
    if status in ACTIVE_STATES:
        return "active"
    if status in FAILED_STATES:
        return "failed"
    if status in ("processing", "pending", "reviewing", "auditing", "wait",
                  "waiting", "uploading", "created"):
        return "pending"
    return "unknown"


def cached(run: Run, key: str) -> int | None:
    """Ledger assets lookup. Keys are ``video``/``image`` for the old single
    path, or ``video:v1``/``image:i1`` for gen multi-asset rounds."""
    value = run.assets.get(key)
    return int(value) if value else None


def upload(client: Mglc, path: str | Path, kind: str, *, name: str | None = None) -> int:
    """Register one file; returns the asset id (review may still be pending)."""
    path = Path(path)
    if not path.is_file():
        raise AssetError(f"nothing to upload at {path}")
    envelope: Envelope = client.asset_upload(path, name or path.stem, kind)
    asset_id = extract_asset_id(envelope.data)
    return asset_id


def wait_active(client: Mglc, asset_id: int, *, timeout: float = 1800.0,
                interval: float = 15.0, log=None) -> str:
    """Poll ``virtual-ip info`` until the asset is reviewable. Returns the status.

    An answer that says nothing about review state fails immediately rather than
    being retried: polling cannot fix a reply we cannot read, and the raw payload is
    what someone needs to see to fix the reader.
    """
    deadline = time.monotonic() + timeout
    last = ""
    while True:
        envelope = client.asset_info(asset_id)
        last = status_of(envelope.data)
        verdict = classify(last) if last else "unknown"
        if verdict == "active":
            return last
        if verdict == "failed":
            raise AssetError(f"asset {asset_id} was rejected by review (status {last!r})")
        if verdict == "unknown":
            raise AssetError(
                f"asset {asset_id}: the review status is unreadable, so waiting cannot "
                f"help. Raw payload:\n  {envelope.data!r}"
            )
        if time.monotonic() >= deadline:
            raise AssetError(
                f"asset {asset_id} was still {last or 'unreported'} after {timeout:g}s "
                f"(last status {last!r}); raise aigc.review_timeout_s or check the "
                f"console"
            )
        if log is not None:
            log.info(f"asset {asset_id}: {last} -- waiting {interval:g}s")
        time.sleep(interval)


def ensure_assets(client: Mglc, run: Run, *,
                  refs: list[tuple[str, str, str | Path]],
                  poll: bool = True, timeout: float = 1800.0, interval: float = 15.0,
                  log=None) -> dict[str, int]:
    """Upload + approve whatever this run needs, verifying anything already recorded.

    ``refs`` is the submission manifest as ``[(key, kind, path)]`` where ``key`` is
    the ledger key (``video:v1`` / ``image:i1`` / ``image:mask``). Three flows share
    the same form since decision 33.

    **Recorded ids are verified, not trusted.** They are reusable across rounds -- but
    only within the same account: the first real "does not belong to the current
    enterprise" (code -400) came from a round whose asset ids were registered before a
    re-login, and the ledger happily carried them all the way to the quota-spending
    call. So a recorded id is re-checked against the service first, and a fresh upload
    is checked too (mglc keeps its own local upload cache, which can hand back an
    object from the previous account). Failing here costs nothing; failing at submit
    costs the round.
    """
    wanted = [(key, kind, Path(path)) for key, kind, path in refs]

    for key, kind, path in wanted:
        existing = cached(run, key)
        if existing is not None:
            state = asset_state(client, existing)
            if state == "ok":
                if log is not None:
                    log.info(f"{key} asset {existing} still valid")
                continue
            if state == "pending":
                if poll:
                    wait_active(client, existing, timeout=timeout, interval=interval,
                                log=log)
                continue
            if log is not None:
                log.warn(f"{key} asset {existing} is {state} for this account; "
                         f"re-registering {path.name}")
            run.assets.pop(key, None)
            run.note(f"asset {existing} no longer usable ({state}); re-registered")
            run.save()

        asset_id = upload(client, path, kind, name=f"{key.replace(':', '_')}_{path.stem}")
        state = asset_state(client, asset_id)
        if state in ("stale", "failed", "unreadable"):
            raise AssetError(
                f"the upload returned asset {asset_id}, which this account cannot use "
                f"({state}).\n"
                f"  mglc keeps its own local upload cache, which may still point at an "
                f"object registered by a previous account; clear it and retry:\n"
                f"    {_upload_cache_hint()}"
            )
        run.assets[key] = asset_id
        run.note(f"uploaded {key} {path.name} -> asset {asset_id}")
        run.save()
        if log is not None:
            log.step(f"uploaded {key} {path.name} -> asset {asset_id}")
        if poll:
            status = wait_active(client, asset_id, timeout=timeout, interval=interval,
                                 log=log)
            run.note(f"asset {asset_id} review: {status}")
            run.save()
    return dict(run.assets)


def asset_state(client: Mglc, asset_id: int) -> str:
    """``"ok"`` | ``"pending"`` | ``"stale"`` | ``"failed"`` | ``"unreadable"``.

    Two ways an asset is not ours any more, and both must be caught here: the service
    refuses to describe it at all (*stale*), or it describes it without saying anything
    about review state (*unreadable*) -- which is how the first foreign id actually
    presented itself, ``code 0`` and a status-less payload.
    """
    try:
        data = client.asset_info(int(asset_id)).data
    except MglcError:
        return "stale"
    status = status_of(data)
    if not status:
        return "unreadable"
    verdict = classify(status)
    if verdict == "active":
        return "ok"
    if verdict == "pending":
        return "pending"
    if verdict == "failed":
        return "failed"
    return "unreadable"           # an unrecognised status is not a usable one


def _upload_cache_hint() -> str:
    """Where mglc caches "this file is already in COS", on Windows and POSIX."""
    home = Path.home() / ".mglc" / "upload_cache.json"
    return str(home)
