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
from ..prep.budget import InputSpec, check_image
from .ledger import Run, RunStore, run_key, write_command
from .task import extract_task_ids, result_urls
from .task import wait as wait_task

IMAGE_TIERS = ("1K", "2K", "4K")

#: 图片步参考图的职责说明；模板里的 ``{image_roles}`` 由它按上传顺序展开。
IMAGE_ROLE_TEXT = {
    "first_frame_raw": "首帧原始图（场景、机位与占位位置底图）",
    "last_frame_raw": "尾帧原始图（场景、机位与占位位置底图）",
    "subject_ref": "人物三视图（唯一外观依据）",
    "first_frame": "首帧定稿图（同一人物的身份锚点）",
    "last_frame": "尾帧定稿图（同一人物的身份锚点）",
    "detail_ref": "细节参考图（只继承所指细节的外观，不取构图、背景与姿态）",
    "edit_master": "待重绘的 360 底图（本轮唯一要修改的底图）",
    "mask": "掩码图（黑色区域需要重绘，白色区域保持原样）",
    "lighting_ref": "氛围与光影参考图（只取光源位置、色温、明暗对比与空气质感，不取构图、背景、人物与画面内容）",
}


def image_roles_text(roles: list[str] | tuple[str, ...],
                     notes: list[str] | tuple[str, ...] | None = None) -> str:
    """把参考图顺序与角色展开成「图1 = …；图2 = …」的分工说明。

    ``notes`` 与 ``roles`` 按位置对应；空字符串表示这张图没有额外用途。
    用途末尾的句号由这里统一补，调用方不用写。
    """
    note_list = list(notes or ())
    lines = []
    for index, role in enumerate(roles, start=1):
        text = IMAGE_ROLE_TEXT.get(role, role)
        note = str(note_list[index - 1]).strip().rstrip("。") if index <= len(note_list) else ""
        line = f"图{index} = {text}"
        if note:
            line += f"：{note}"
        lines.append(line + "。")
    return "\n".join(lines)


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
    step: str = ""


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
    if pkg.step:
        params["step"] = str(pkg.step)
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
        _check_input_images(conf, pkg.images, detail.data)
        argv = build_image_argv(
            prompt=pkg.prompt, model_id=pkg.model_id, images=list(pkg.images),
            ratio=pkg.ratio, resolution=pkg.resolution, num=pkg.num,
            session_id=pkg.session_id)
        write_command(run, [client.exe(), "image", "generate", *argv], spend=True)
        log.step("submitting image round (this spends quota)")
        envelope = client.image_generate(
            argv, confirm=True, timeout=float(conf.get("mglc.poll_timeout_s", 3600.0)))
        task_ids = extract_task_ids(envelope.data)
        run.task_id = task_ids[0]
        run.task_ids = task_ids
        run.advance("submitted")
        run.note("submitted as image task(s) "
                 + ", ".join(str(item) for item in task_ids))
        run.save()

    if run.results:
        results = tuple(Path(p) for p in run.results)
        expected = len(run.task_ids or [run.task_id]) if run.task_id else 0
        if all(p.is_file() for p in results) and len(results) >= expected:
            return ImageOutcome(run, results, run.gate or {})
    else:
        ids = list(run.task_ids or [run.task_id])
        try:
            for task_id in ids:
                wait_task(client, int(task_id),
                          interval=float(conf.get("mglc.poll_interval_s", 20.0)),
                          timeout=float(conf.get("mglc.poll_timeout_s", 3600.0)),
                          log=log)
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


def _ref_image_validator(node: Any) -> Mapping[str, object]:
    """Find the first ``refImageValidator`` block in a model detail payload."""
    if isinstance(node, Mapping):
        candidate = node.get("refImageValidator")
        if isinstance(candidate, Mapping):
            return candidate
        for value in node.values():
            found = _ref_image_validator(value)
            if found:
                return found
    elif isinstance(node, (list, tuple)):
        for item in node:
            found = _ref_image_validator(item)
            if found:
                return found
    return {}


def _check_input_images(conf, images: tuple[Path, ...], detail: Any) -> None:
    """Fail before submission when an input image exceeds the model's limits.

    The image path used to pass paths straight to ``mglc``; a validator block in
    the model detail is the platform's own statement of the caps, so checking it
    here turns a paid rejection into a local error naming the failing side.
    """
    validator = _ref_image_validator(detail)
    if not validator:
        return
    for image in images:
        spec = probe_image(str(image), conf)
        report = check_image(spec, validator)
        if not report.ok:
            failures = "; ".join(check.line() for check in report.failures)
            raise UsageError(f"输入图超出模型限额：{image}：{failures}")


def _fetch_all(client: Mglc, run: Run, *, opener=None, log=None) -> tuple[Path, ...]:
    if run.task_id is None:
        raise AigcError(f"run {run.key} was never submitted")
    urls: list[str] = []
    payloads: list[Any] = []
    for task_id in run.task_ids or [run.task_id]:
        data = client.task_status(int(task_id)).data
        payloads.append(data)
        for url in result_urls(data, include_marked=False) or result_urls(data):
            if url not in urls:
                urls.append(url)
    if not urls:
        raise AigcError(
            "the image task reports success but carries no result URL; raw payloads:\n"
            f"  {payloads!r}")
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
