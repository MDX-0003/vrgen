"""Image generation as its own task kind.

The platform's image command is a sibling of ``video generate``, not a flag on it:
the two subcommands have different parameter vocabularies (``--ratio`` and
``1K/2K/4K`` vs ``--aspect-ratio`` and ``480p/720p/1080p``) and images need no
SD-compliance asset ids. This module keeps that vocabulary in one builder, and
implements the same ledger lifecycle as video rounds so ``status`` / ``result``
do not need a second code path.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import AigcError, UsageError
from ..paths import sha256_16
from ..platform.client import Mglc
from ..platform.media import probe_image
from ..prep.budget import InputSpec
from .ledger import Run, RunStore, run_key, write_command
from .task import extract_task_id, result_urls
from .task import wait as wait_task

IMAGE_TIERS = ("1K", "2K", "4K")


def build_image_argv(*, prompt: str, model_id: int, images: list[str | Path],
                     ratio: str, resolution: str, num: int,
                     session_id: int | None = None) -> list[str]:
    """The ``mglc image generate`` arguments for one round."""
    if not images:
        raise AigcError("image generate needs at least one reference image")
    if resolution not in IMAGE_TIERS:
        raise UsageError(f"--resolution 只能是 {' | '.join(IMAGE_TIERS)}")
    if not str(ratio).strip():
        raise UsageError("--ratio 不能为空")
    if not 1 <= int(num) <= 4:
        raise UsageError("--num 必须在 1..4")
    argv = ["--prompt", prompt,
            "--model-id", str(int(model_id)),
            "--ratio", str(ratio),
            "--resolution", str(resolution),
            "--num", str(int(num))]
    for image in images:
        argv += ["--image", str(image)]
    if session_id:
        argv += ["--session-id", str(int(session_id))]
    return argv


@dataclass(frozen=True)
class ImagePackage:
    """Everything the CLI knows before the local half of an image round."""

    job: str
    prompt: str
    model_id: int
    images: tuple[Path, ...]
    ratio: str
    resolution: str
    num: int
    workdir: Path
    session_id: int = 0


@dataclass(frozen=True)
class ImagePrepared:
    run: Run
    package: ImagePackage
    reused: bool = False


@dataclass(frozen=True)
class ImageOutcome:
    run: Run
    results: tuple[Path, ...]
    gate: dict[str, Any]


def prepare_image_round(conf, pkg: ImagePackage, *, log) -> ImagePrepared:
    """Validate, create the ledger round, and keep the candidate list on disk."""
    if not pkg.prompt.strip():
        raise UsageError("--prompt 不能为空")
    if not pkg.images:
        raise UsageError("图片生成至少需要一张 --image")
    for image in pkg.images:
        if not Path(image).is_file():
            raise UsageError(f"图片不存在：{image}")
    if pkg.resolution not in IMAGE_TIERS:
        raise UsageError(f"--resolution 只能是 {' | '.join(IMAGE_TIERS)}")
    if not 1 <= int(pkg.num) <= 4:
        raise UsageError("--num 必须在 1..4")

    params: dict[str, Any] = {
        "backend": "mglc",
        "kind": "image",
        "model_id": int(pkg.model_id),
        "ratio": str(pkg.ratio),
        "resolution": str(pkg.resolution),
        "num": int(pkg.num),
        "session_id": int(pkg.session_id),
        "images": [str(p) for p in pkg.images],
    }
    extra = {"images": [sha256_16(p) for p in pkg.images]}
    key = run_key(pkg.prompt, pkg.images[0], None, params, extra)
    store = RunStore(pkg.workdir)
    existing = store.find_by_key(key)
    if existing is not None:
        log.warn(f"this image round already exists: {existing.dir.name} "
                 f"(stage {existing.stage}) -- reusing it")
        return ImagePrepared(existing, pkg, reused=True)

    run = store.create(key, label="img", params=params, prompt=pkg.prompt,
                       upload=pkg.images[0])
    log.step(f"round {run.dir.name}")
    run.save()
    log.info(f"prompt -> {run.prompt_path}")
    return ImagePrepared(run, pkg)


def execute_image_round(conf, pkg: ImagePackage, prepared: ImagePrepared, *,
                        client: Mglc, log, opener=None) -> ImageOutcome:
    """Submit, poll, download every candidate, and record the generic gate."""
    run = prepared.run
    if run.task_id is None:
        detail = client.model_detail(int(pkg.model_id), model_type="image")
        _validate_model_detail(detail.data, pkg.ratio, pkg.resolution)
        argv = build_image_argv(
            prompt=pkg.prompt, model_id=pkg.model_id, images=list(pkg.images),
            ratio=pkg.ratio, resolution=pkg.resolution, num=pkg.num,
            session_id=pkg.session_id)
        write_command(run, [client.exe(), "image", "generate", *argv], spend=True)
        log.step("submitting image round (this spends quota)")
        envelope = client.image_generate(
            argv, confirm=True, timeout=float(conf.get("mglc.poll_timeout_s", 3600.0)))
        task_id = extract_task_id(envelope.data)
        run.task_id = task_id
        run.advance("submitted")
        run.note(f"submitted as image task {task_id}")
        run.save()

    if run.results:
        results = tuple(Path(p) for p in run.results)
        if all(p.is_file() for p in results):
            return ImageOutcome(run, results, run.gate or {})
    else:
        try:
            wait_task(client, int(run.task_id),
                      interval=float(conf.get("mglc.poll_interval_s", 20.0)),
                      timeout=float(conf.get("mglc.poll_timeout_s", 3600.0)), log=log)
        except AigcError:
            run.advance("failed")
            run.save()
            raise
        run.advance("succeeded")
        run.save()

    results = _fetch_all(client, run, opener=opener, log=log)
    gates: list[dict[str, Any]] = []
    verdict = "ok"
    for path in results:
        try:
            spec = probe_image(path, conf)
            gates.append({"path": str(path), "width": spec.width,
                          "height": spec.height, "size_bytes": spec.size_bytes})
        except AigcError as exc:
            verdict = "error"
            gates.append({"path": str(path), "error": str(exc)})
    run.results = [str(p) for p in results]
    run.gate = {"verdict": verdict, "kind": "image", "images": gates}
    run.advance("checked")
    run.save()
    return ImageOutcome(run, results, run.gate)


def gate_image(path: str | Path, config=None) -> InputSpec:
    """Generic image gate: the file decodes and has a usable raster."""
    return probe_image(path, config)


def _strings_under(node: Any, key: str) -> list[str]:
    """Collect every string in a list published under ``key``, wherever nested."""
    found: list[str] = []
    if isinstance(node, Mapping):
        for name, value in node.items():
            if name == key and isinstance(value, (list, tuple)):
                found.extend(str(item) for item in value)
            else:
                found.extend(_strings_under(value, key))
    elif isinstance(node, (list, tuple)):
        for item in node:
            found.extend(_strings_under(item, key))
    return found


def _validate_model_detail(data: Any, ratio: str, resolution: str) -> None:
    """Check the requested options against the model detail when it publishes them.

    The image model detail shape is less documented than the video list, so this
    is deliberately tolerant: only a published list constrains the request.
    """
    ratios = _strings_under(data, "ratios")
    if ratios and ratio not in ratios:
        raise UsageError(f"图片模型只接受这些比例：{', '.join(ratios)}；当前是 {ratio!r}")
    resolutions = _strings_under(data, "resolutions")
    if resolutions and resolution not in resolutions:
        raise UsageError(
            f"图片模型只接受这些分辨率档：{', '.join(resolutions)}；当前是 {resolution!r}")


def _fetch_all(client: Mglc, run: Run, *, opener=None, log=None) -> tuple[Path, ...]:
    if run.task_id is None:
        raise AigcError(f"run {run.key} was never submitted")
    data = client.task_status(int(run.task_id)).data
    urls = result_urls(data, include_marked=False) or result_urls(data)
    if not urls:
        raise AigcError(
            "the image task reports success but carries no result URL; raw payload:\n"
            f"  {data!r}")
    open_url = opener or urllib.request.urlopen
    results: list[Path] = []
    for index, url in enumerate(urls, start=1):
        suffix = Path(url.split("?")[0]).suffix or ".png"
        target = run.dir / f"result_{index}{suffix}"
        if log is not None:
            log.info(f"downloading candidate {index}: {url}")
        try:
            with open_url(url, timeout=600.0) as response:
                target.write_bytes(response.read())
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise AigcError(f"could not download {url}: {exc}") from exc
        results.append(target)
    run.note(f"downloaded {len(results)} image candidate(s)")
    return tuple(results)
