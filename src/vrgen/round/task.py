"""Submit, watch, and collect one generation task.

``build_generate_argv`` is the single place that decides what a request looks like.
That is on purpose: the platform's parameter names have already contradicted each
other across channels once (the CLI's ``--resolution`` help says ``1K/2K/4K``, which
is the *image* wording, while every video model advertises ``480p/720p/1080p``), so
there is exactly one function to fix and one file to check when the service
disagrees with us.

The task lifecycle is small and the CLI exposes only a status query: states 0-4,
no cancel, no blocking wait, and no downloader -- the result URL has to be fetched
by the caller.

**Response shapes are unverified** (no authenticated call yet). Task-id and result
URL extraction search the payload rather than index it, and fail loudly with the raw
JSON so the first real run resolves them.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..errors import AigcError, TaskFailed
from ..platform.client import Mglc, walk_scalars
from .ledger import Run, assert_spendable, write_command

#: The CLI's own state codes.
TASK_STATES = {0: "submitted", 1: "running", 2: "succeeded", 3: "failed", 4: "cancelled"}
TERMINAL_GOOD = 2
TERMINAL_BAD = (3, 4)

_TASK_ID_KEYS = ("taskid", "id")
_URL_HINTS = (".mp4", ".mov", ".m4v", ".png", ".jpg", ".jpeg", ".webp")
_WM_MARKERS = ("wm", "watermark", "watermarked")


def build_generate_argv(*, prompt: str, model_id: int, mode: str, duration_s: int,
                        resolution: str, aspect_ratio: str, num: int,
                        videos: list[tuple[str | Path, int | None]] | None = None,
                        images: list[tuple[str | Path, int | None]] | None = None,
                        first_image: tuple[str | Path, int | None] | None = None,
                        last_image: tuple[str | Path, int | None] | None = None,
                        session_id: int | None = None,
                        keep_sound: bool = True,
                        preview: bool = False) -> list[str]:
    """The ``mglc video generate`` arguments for one round.

    两种输入形状共享这个 builder：

    * ``videos`` / ``images`` -- 提交清单，每项 ``(path, asset_id)``，按提示词
      编号顺序（视频在前、图片在后，mask 追加在图片末尾）；
    * ``first_image`` / ``last_image`` -- firstLastFrame 的两张固定帧。它们映射到
      ``--first-image`` / ``--last-image``，不产生 ``--video`` / ``--image`` /
      ``--asset-id`` 对。

    ``--duration`` is passed even for an editing task, where the platform reports
    ``duration.type = ""`` (i.e. the timeline is the input's): the CLI marks the flag
    required, so omitting it fails client-side before the server can arbitrate.

    ``keep_sound`` defaults on, and it is not cosmetic: the padded file carries the
    source's audio, ``recompose`` copies the audio of whatever the model returned,
    and without this flag the model returns a **silent** file -- so the deliverable
    loses its soundtrack at this link. Measured on the first real round (2026-09-23):
    source aac -> padded aac -> returned nothing -> final nothing.

    ``preview=True`` substitutes placeholders for missing asset ids instead of
    refusing, so the command can be shown to a human before anything was uploaded.
    The real path stays strict: a request without its SD-compliance ids is rejected
    by the service, and finding that out from a human reading the command is cheaper
    than finding out from a failed task.
    """
    argv = ["--prompt", prompt,
            "--model-id", str(int(model_id)),
            "--type", str(mode),
            "--duration", str(int(duration_s)),
            "--resolution", str(resolution),
            "--num", str(int(num))]

    if mode == "firstLastFrame":
        if videos or images:
            raise AigcError("firstLastFrame only accepts first_image/last_image; "
                            "video and ordinary image references are not allowed")
        if first_image is None or last_image is None:
            raise AigcError("firstLastFrame needs both a first frame and a last frame")
        # 输出比例跟随首尾图；模型详情里该模式 ratios 为空，不传 --aspect-ratio。
        argv += ["--first-image", str(first_image[0]),
                 "--last-image", str(last_image[0])]
        if session_id:
            argv += ["--session-id", str(int(session_id))]
        return argv

    videos = videos or []
    images = images or []
    if not videos and not images:
        raise AigcError("a generation round needs at least one reference video or image")

    # videoEdit 的输出比例跟随输入（模型详情里 ratios 为空）；"auto" 表示不传该 flag。
    if str(aspect_ratio).strip().lower() != "auto":
        argv += ["--aspect-ratio", str(aspect_ratio)]
    if keep_sound:
        argv.append("--keep-original-sound")
    for path, asset_id in videos:
        argv += ["--video", str(path)]
        if asset_id is None:
            if not preview:
                raise AigcError("Seedance requires a matching SD-compliance asset id for "
                                "every reference video; run `vrgen submit` again after the "
                                "asset step")
            asset_id = "<video-asset-id>"
        argv += ["--video-asset-id", str(asset_id)]
    if session_id:
        # 项目生成会话：平台的计费上下文跟随会话，额度走项目而不是个人。
        argv += ["--session-id", str(int(session_id))]
    for path, asset_id in images:
        argv += ["--image", str(path)]
        if asset_id is None:
            if not preview:
                raise AigcError("the reference image also needs an asset id for Seedance; "
                                "run `vrgen submit` again after the asset step")
            asset_id = "<image-asset-id>"
        argv += ["--asset-id", str(asset_id)]
    return argv


def extract_task_id(data: Any) -> int:
    """Pull the task id out of a submit response, wherever it is nested."""
    candidates: list[tuple[int, int]] = []
    for key, value in walk_scalars(data):
        low = key.lower()
        for rank, wanted in enumerate(_TASK_ID_KEYS):
            if low == wanted or low.endswith(wanted):
                try:
                    candidates.append((rank, int(value)))
                except (TypeError, ValueError):
                    pass
                break
    if not candidates:
        raise AigcError(
            "no task id in the submit response (shape unverified -- this is the first "
            "authenticated run's job to pin down); raw payload:\n"
            f"  {data!r}"
        )
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def submit(client: Mglc, run: Run, *, confirm: bool = False, timeout: float = 300.0,
           log=None) -> int:
    """Send the request. The only quota-spending call in the package.

    Guards itself against a second spend on the same round: this is the function that
    spends, so this is where the refusal belongs. The pipeline never reaches it twice
    (it only calls when the ledger has no task id), but a direct library call cannot
    double-charge either.
    """
    assert_spendable(run)
    refs = run.params.get("refs") or []
    if not refs:
        raise AigcError(f"run {run.key} has no prepared refs; run `vrgen submit` again")
    mode = str(run.params["mode"])
    first_image = last_image = None
    if mode == "firstLastFrame":
        by_role = {str(item.get("role")): item for item in refs}
        first = by_role.get("first_frame")
        last = by_role.get("last_frame")
        if first is None or last is None:
            raise AigcError("firstLastFrame round is missing its first_frame or last_frame")
        first_image = (str(first["upload"]), None)
        last_image = (str(last["upload"]), None)
        videos: list[tuple[str | Path, int | None]] = []
        images: list[tuple[str | Path, int | None]] = []
    else:
        videos = [(str(item["upload"]), run.assets.get(f"video:{item['id']}"))
                  for item in refs if item.get("kind") == "video"]
        # mask 固定追加在所有图片之后；其余图片保持清单顺序。
        images = [(str(item["upload"]), run.assets.get(f"image:{item['id']}"))
                  for item in refs
                  if item.get("kind") == "image" and item.get("role") != "mask"]
        images += [(str(item["upload"]), run.assets.get(f"image:{item['id']}"))
                   for item in refs if item.get("role") == "mask"]
    argv = build_generate_argv(
        prompt=run.prompt,
        model_id=int(run.params["model_id"]),
        mode=mode,
        duration_s=int(run.params["duration_s"]),
        resolution=str(run.params["resolution"]),
        aspect_ratio=str(run.params["aspect_ratio"]),
        num=int(run.params.get("num", 1)),
        videos=videos,
        images=images,
        first_image=first_image,
        last_image=last_image,
        keep_sound=bool(run.params.get("keep_sound", True)),
        session_id=int(run.params.get("session_id", 0) or 0),
    )
    write_command(run, [client.exe(), "video", "generate", *argv], spend=True)
    if log is not None:
        log.step("submitting (this spends quota)")
    envelope = client.generate(argv, confirm=confirm, timeout=timeout)
    task_id = extract_task_id(envelope.data)
    run.task_id = task_id
    run.advance("submitted")
    run.note(f"submitted as task {task_id}")
    run.save()
    return task_id


def state(client: Mglc, task_id: int) -> tuple[int, str]:
    return parse_state(client.task_status(task_id).data)


def parse_state(data: Any) -> tuple[int, str]:
    for key, value in walk_scalars(data):
        if key.lower().endswith("status") or key.lower() == "state":
            try:
                code = int(value)
            except (TypeError, ValueError):
                continue
            return code, TASK_STATES.get(code, f"unknown({code})")
    raise AigcError(f"no task status in the response (shape unverified); raw:\n  {data!r}")


def wait(client: Mglc, task_id: int, *, interval: float = 20.0, timeout: float = 3600.0,
         log=None) -> tuple[int, str]:
    """Poll to a terminal state. A failed or cancelled task raises ``TaskFailed``."""
    deadline = time.monotonic() + timeout
    started = time.monotonic()
    while True:
        code, label = state(client, task_id)
        if code == TERMINAL_GOOD:
            return code, label
        if code in TERMINAL_BAD:
            raise TaskFailed(task_id, label)
        if time.monotonic() >= deadline:
            raise AigcError(
                f"task {task_id} still {label} after {timeout:g}s; it is not cancellable, "
                f"so check it later with `vrpad aigc status --task-id {task_id}`"
            )
        if log is not None:
            log.info(f"task {task_id}: {label}  ({time.monotonic() - started:.0f}s)")
        time.sleep(interval)


def _walk_url_values(node: Any, key: str = ""):
    """Like ``walk_scalars`` but keeps the parent key for scalar list items.

    Image payloads commonly return ``imgUrlList: ["https://…png", …]``; the shared
    walker drops the key while descending into a list, and the URL itself still
    identifies an image by suffix. Keeping the key here lets ``result_urls`` apply
    the same image/video and watermark rules to list-shaped payloads.
    """
    if isinstance(node, Mapping):
        for name, value in node.items():
            yield from _walk_url_values(value, str(name))
    elif isinstance(node, (list, tuple)):
        for item in node:
            yield from _walk_url_values(item, key)
    else:
        yield key, node


def result_urls(data: Any, *, include_marked: bool = True) -> list[str]:
    """Result media URLs, watermarked variants last.

    The platform returns both ``videoUrl`` and ``wmVideoUrl`` for the same render.
    The watermarked one must not be picked: the endpoint lock at the start and end
    of the recomposed panorama adopts raw pixels, so a watermark would land in the
    deliverable. ``include_marked=False`` is for multi-candidate image rounds,
    where every unwatermarked URL is a separate candidate and the watermarked
    duplicates are not candidates at all.
    """
    plain: list[str] = []
    marked: list[str] = []
    for key, value in _walk_url_values(data):
        if not isinstance(value, str) or not value.startswith(("http://", "https://")):
            continue
        low_key, low_value = key.lower(), value.lower().split("?")[0]
        if not (any(low_value.endswith(ext) for ext in _URL_HINTS)
                or "url" in low_key or "video" in low_key or "image" in low_key):
            continue
        bucket = marked if any(marker in low_key for marker in _WM_MARKERS) else plain
        bucket.append(value)
    seen: set[str] = set()
    ordered: list[str] = []
    for url in (*plain, *marked):
        if url not in seen:
            seen.add(url)
            ordered.append(url)
    if include_marked:
        return ordered
    marked_set = set(marked)
    return [url for url in ordered if url not in marked_set]


def fetch(client: Mglc, run: Run, *, timeout: float = 600.0, opener=None,
          log=None) -> Path:
    """Download the best (non-watermarked) result into the run directory.

    ``opener`` defaults to :func:`urllib.request.urlopen` and exists so the download
    can be exercised offline -- every other external call in this package has an
    injection point, and a test that needs the network is a test that does not run.
    """
    if run.task_id is None:
        raise AigcError(f"run {run.key} was never submitted")
    data = client.task_status(int(run.task_id)).data
    urls = result_urls(data)
    if not urls:
        code, label = parse_state(data)
        if code != TERMINAL_GOOD:
            raise AigcError(f"task {run.task_id} is {label}: there is nothing to "
                            f"download yet. Re-run the same command to wait for it.")
        raise AigcError(
            "the task reports success but carries no result URL; raw payload:\n"
            f"  {data!r}"
        )
    url = urls[0]
    suffix = Path(url.split("?")[0]).suffix or ".mp4"
    target = run.dir / f"result{suffix}"
    if log is not None:
        log.info(f"downloading {url}")
    open_url = opener or urllib.request.urlopen
    try:
        with open_url(url, timeout=timeout) as response:
            target.write_bytes(response.read())
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AigcError(f"could not download {url}: {exc}") from exc
    run.result = str(target)
    run.advance("fetched")
    run.note(f"downloaded {target.name} from {url}")
    run.save()
    if log is not None:
        log.step(f"{target}  ({target.stat().st_size / 1e6:.1f} MB)")
    return target
