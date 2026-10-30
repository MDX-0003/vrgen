"""The one place the stages are ordered. No CLI vocabulary lives here.

← vrpad aigc/round/pipeline.py 迁入并按 vrgen 契约改写两处：

1. **输入不再来自 case 发现，而是显式输入包**（``InputPackage``）——canvas/source
   尺寸由调用方（vrstudio generate，它手里有 manifest）传入，vrgen 不读任何
   几何契约文件。几何数字进提示词的机制不变（``UploadGeometry`` → 占位符）。
2. **末段闸门换通用闸门**（决策 D3）：vrpad 在这里跑带相关配准（几何闸门），
   那属于 vrstudio 的 recompose；vrgen 只验证"结果能解码、尺寸/帧数在册"。

阶段不变：prepared（本地免费）→ assets（平台状态）→ submitted（花钱、不可逆）
→ succeeded（轮询）→ fetched（结果落盘）→ checked（通用闸门入账）。

决策 33 起只有一条 local half：提交 = ``--refs`` 清单（三个流程同构）。流程差异
（母版、mask、几何）都已由调用方折算进清单与 canvas/source；这里不感知 flow。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .. import refs as refs_mod
from ..errors import AigcError, BudgetError, LedgerError, TaskFailed, UsageError
from ..paths import sha256_16, templates_dir
from ..platform.client import Mglc
from ..platform.media import (
    probe_image,
    probe_input,
    scale_image,
    scale_reference_image,
    scale_video,
)
from ..platform.models import ModeLimits, ModelInfo, find_model, load_models
from ..prep.budget import InputSpec, check_image, check_input, fit_image_size
from ..prep.params import effective_aspect
from ..prep.prompt import geometry_variables, render, resolve_template
from ..prep.scale import (
    UploadGeometry,
    choose_upload_size,
    parse_size_mode,
    plain_geometry,
    scaled_geometry,
)
from .assets import ensure_assets
from .ledger import STAGES, Run, RunStore, failed_round_hint, run_key
from .task import fetch as fetch_result
from .task import submit as submit_task
from .task import wait as wait_task

assert STAGES, "stage names live in ledger.py (the lifecycle owner); imported here"


@dataclass(frozen=True)
class InputPackage:
    """一轮生成的全部显式输入（契约：docs/case-model.md §4 的 submit 参数）。

    ``refs`` 是唯一素材入口，顺序即 ``[submit] selected`` 的顺序（mask 由装配器
    追加在最后）。``canvas_size``/``source_size`` 是提示词几何占位符的唯一来源；
    省略时按母版推导（gen 流）。``band_w = canvas_w - source_w``，两者相等时
    没有冗余带。
    """

    job: str
    workdir: Path                      # 台账根（runs/）
    template: str                      # 模板 id（templates/<id>.md）
    variables: dict[str, str]          # prompt-vars 的 [vars]（含本次 --set 合并）
    refs: tuple[refs_mod.RefAsset, ...]
    canvas_size: tuple[int, int] | None = None
    source_size: tuple[int, int] | None = None


@dataclass(frozen=True)
class Prepared:
    """Everything the local half produced, plus what the remote half will need."""

    model: ModelInfo
    limits: ModeLimits
    geometry: UploadGeometry
    spec: InputSpec
    run: Run
    reused: bool = False               # this key already had a run directory


@dataclass
class Outcome:
    """What the remote half did. Every fact is also in the ledger; this is a view."""

    run: Run
    result: Path | None = None
    gate: dict | None = None
    assets: dict[str, int] = field(default_factory=dict)
    submitted: int | None = None       # task id, only when THIS call submitted
    rounds: list[str] = field(default_factory=list)   # ledger summary, for the console


# ---- local half ------------------------------------------------------------
def _duration_s(conf, spec: InputSpec) -> int:
    """The integer seconds the platform wants: 0 in config means 'derive it'."""
    configured = int(conf.get("mglc.duration_s", 0) or 0)
    if configured:
        return configured
    seconds = spec.duration_s if spec.duration_s > 0 else 0.0
    return max(4, int(seconds) + (1 if seconds % 1 else 0))


def _duration_range(limits: ModeLimits) -> tuple[int, int] | None:
    duration = dict(limits.duration or {})
    if duration.get("type") != "range":
        return None
    try:
        return int(duration["min"]), int(duration["max"])
    except (KeyError, TypeError, ValueError):
        return None


def _gen_duration(conf, limits: ModeLimits, *, source: str,
                  master_spec: InputSpec,
                  planned: list[tuple[refs_mod.RefAsset, InputSpec, tuple[int, int], bool]]
                  ) -> tuple[int, str]:
    """Resolve a round's duration: explicit > longest video > default 4."""
    configured = int(conf.get("mglc.duration_s", 0) or 0)
    if configured:
        span = _duration_range(limits)
        if span is not None and not span[0] <= configured <= span[1]:
            raise UsageError(
                f"--duration {configured} 超出模式 {limits.mode} 的合法区间 "
                f"{span[0]}-{span[1]} 秒")
        return configured, source
    if source == "longest":
        videos = [p[1] for p in planned if p[0].kind == "video"]
        if videos:
            return _duration_s(conf, max(videos, key=lambda spec: spec.duration_s)), source
        return 4, "none"
    if source == "none":
        return 4, "none"
    return _duration_s(conf, master_spec), source


def _parse_wh(text: str, what: str) -> tuple[int, int]:
    left, _, right = str(text).lower().partition("x")
    try:
        w, h = int(left), int(right)
    except ValueError as exc:
        raise AigcError(f"{what} {text!r} 不是 WxH（如 4668x2000）") from exc
    if w < 2 or h < 2:
        raise AigcError(f"{what} {text!r}：两边都要 >= 2")
    return w, h


def prepare_round(conf, pkg: InputPackage, *, log) -> Prepared:
    """The local half: build the upload artifacts, the prompt, and the ledger.

    Idempotent by run key: same inputs + same rendered prompt + same parameters
    resolve to the same key and reuse the first run's directory.
    """
    if not pkg.refs:
        raise UsageError("提交清单为空：勾选要提交的素材后再提交")
    return _prepare_refs(conf, pkg, log=log)


def _prepare_refs(conf, pkg: InputPackage, *, log) -> Prepared:
    """单一本地半场：校验 → 各自缩放 → 母版几何 → 提示词 → 台账。"""
    models = load_models(pkg.workdir, url=str(conf.get("mglc.model_list_url")),
                         max_age_h=float(conf.get("mglc.models_max_age_h", 24.0)), log=log)
    model = find_model(models, int(conf.get("mglc.model_id")))
    limits = model.mode(str(conf.get("mglc.mode")))
    resolution = str(conf.get("mglc.resolution"))
    if limits.resolutions and resolution not in limits.resolutions:
        raise UsageError(
            f"model {model.code} ({model.name}) 的 mode {limits.mode} 只支持这些"
            f"输出档：{' / '.join(limits.resolutions)}；当前是 {resolution!r}")
    refs_mod.validate_mode(pkg.refs, limits.mode)
    refs_mod.validate_counts(pkg.refs, limits.mode,
                             max_videos=limits.max_videos,
                             max_images=limits.max_images)
    aspect = effective_aspect(str(conf.get("mglc.aspect_ratio")), limits.ratios)
    if limits.ratios and aspect not in limits.ratios:
        raise AigcError(
            f"mode {limits.mode} 只接受这些输出比例：{', '.join(limits.ratios)}；"
            f"当前是 {aspect!r}")

    video_validator = limits.ref_video_limits() if limits.takes_video else {}
    image_validator = limits.ref_image_limits()
    mode_name, explicit = parse_size_mode(str(conf.get("mglc.scale")))
    if explicit and sum(1 for a in pkg.refs if a.kind == "video") > 1:
        raise AigcError("多视频提交不支持显式 WxH --scale；用 auto | full | half")
    max_pixels = int(video_validator.get("maxPixel") or 0)
    max_bytes = int(video_validator.get("maxBytes") or 0)

    template_path = resolve_template(f"{pkg.template}.md", templates_dir())
    if not template_path.is_file():
        raise AigcError(f"prompt template not found: {template_path} (templates/<id>.md)")

    # run_key 要等提示词渲染完才算；缩放在这之前完成，落在轮次目录里。先算 key
    # 需要 run 目录，所以先建一个临时 store 目录名——与旧路径一样，用最终 key
    # 建目录；缩放件在目录建好后再写。这里先收集目标尺寸，建 run 后再落盘。
    planned: dict[str, tuple[refs_mod.RefAsset, InputSpec, tuple[int, int], bool]] = {}
    for asset in pkg.refs:
        if asset.role == "mask":
            continue                      # mask 的上传尺寸取决于母版，稍后补
        if asset.kind == "video":
            spec = probe_input(asset.path, conf)
            report = check_input(spec, video_validator)
            labels = {c.label for c in report.failures}
            if labels - {"pixels", "size"}:
                raise BudgetError(
                    f"素材 {asset.id} 不合约：\n"
                    + "\n".join("  " + c.line() for c in report.failures))
            upload_size = (spec.width, spec.height)
            if report.failures:
                upload_size = explicit or choose_upload_size(
                    (spec.width, spec.height), mode=mode_name, spec=spec,
                    max_pixels=max_pixels, max_bytes=max_bytes)
                if upload_size == (spec.width, spec.height):
                    raise BudgetError(
                        f"素材 {asset.id} 在当前 --scale={mode_name} 下仍不合约：\n"
                        + "\n".join("  " + c.line() for c in report.failures))
        else:
            spec = probe_image(asset.path, conf)
            report = check_image(spec, image_validator)
            labels = {c.label for c in report.failures}
            if labels - {"pixels", "size", "width", "height"}:
                raise BudgetError(
                    f"素材 {asset.id} 不合约：\n"
                    + "\n".join("  " + c.line() for c in report.failures))
            upload_size = (spec.width, spec.height)
            if report.failures:
                upload_size = fit_image_size(spec, image_validator)
                if upload_size == (spec.width, spec.height):
                    raise BudgetError(
                        f"素材 {asset.id} 无法通过缩放满足图片限制：\n"
                        + "\n".join("  " + c.line() for c in report.failures))
        planned[asset.id] = (asset, spec, upload_size,
                             upload_size != (spec.width, spec.height))

    master_asset = refs_mod.master(pkg.refs)
    master_plan = planned.get(master_asset.id)
    if master_plan is None:
        raise UsageError("素材清单里只有 mask，没有可用作母版的素材")
    if limits.mode == "firstLastFrame":
        first_plan = planned[next(a.id for a in pkg.refs if a.role == "first_frame")]
        last_plan = planned[next(a.id for a in pkg.refs if a.role == "last_frame")]
        if (first_plan[1].width, first_plan[1].height) != (last_plan[1].width,
                                                           last_plan[1].height):
            raise UsageError(
                "firstLastFrame 的首帧与尾帧宽高必须一致："
                f"{first_plan[1].width}x{first_plan[1].height} vs "
                f"{last_plan[1].width}x{last_plan[1].height}")

    canvas = pkg.canvas_size or (master_plan[1].width, master_plan[1].height)
    source = pkg.source_size or canvas
    if tuple(canvas) == tuple(source):
        geometry = plain_geometry(canvas, master_plan[2])
    else:
        geometry = scaled_geometry(canvas, source, master_plan[2])

    # mask 是内部控制图：强制缩放到母版的上传画布，不按图片预算 fit。
    for asset in pkg.refs:
        if asset.role != "mask":
            continue
        spec = probe_image(asset.path, conf)
        upload_size = tuple(geometry.canvas_size)
        # 体积在缩放重编码后才成立，这里只看宽高、像素与比例。
        report = check_image(InputSpec(width=upload_size[0], height=upload_size[1],
                                       fps=0.0, duration_s=0.0,
                                       size_bytes=0, container=spec.container),
                             image_validator)
        if not report.ok:
            raise BudgetError(
                f"mask {asset.id} 缩放到母版上传画布 {upload_size[0]}x{upload_size[1]} "
                f"后仍不合约：\n"
                + "\n".join("  " + c.line() for c in report.failures))
        planned[asset.id] = (asset, spec, upload_size, upload_size != (spec.width, spec.height))

    variables: dict[str, object] = dict(geometry_variables(geometry))
    variables.update(pkg.variables)
    variables.update(refs_mod.mode_variables(limits.mode))
    variables["asset_list"] = refs_mod.asset_list(pkg.refs)
    prompt = render(template_path.read_text(encoding="utf-8"), variables)

    ordered = [planned[a.id] for a in pkg.refs]
    duration_source = refs_mod.duration_source(limits.mode)
    duration_s, duration_source = _gen_duration(
        conf, limits, source=duration_source, master_spec=master_plan[1], planned=ordered)

    refs_params = []
    for asset, _spec, upload_size, _needs_scale in ordered:
        refs_params.append({
            "id": asset.id,
            "kind": asset.kind,
            "role": asset.role,
            "path": str(asset.path),
            "sha256": asset.sha256 or sha256_16(asset.path),
            "upload": str(asset.path),
            "width": upload_size[0],
            "height": upload_size[1],
        })
    params = {
        "backend": "mglc",
        "model_id": model.code,
        "mode": limits.mode,
        "resolution": str(conf.get("mglc.resolution")),
        "duration_s": duration_s,
        "aspect_ratio": aspect,
        "num": int(conf.get("mglc.num", 1)),
        "session_id": int(conf.get("mglc.session_id", 0)),
        "scale": f"{master_plan[2][0]}x{master_plan[2][1]}",
        "keep_sound": (bool(conf.get("mglc.keep_original_sound", True))
                       and any(a.kind == "video" for a in pkg.refs)),
        "canvas": f"{canvas[0]}x{canvas[1]}",
        "source": f"{source[0]}x{source[1]}",
        "template": pkg.template,
        "refs": refs_params,
        "master": master_asset.id,
        "duration_source": duration_source,
    }

    store = RunStore(pkg.workdir)
    extra = {"refs": [[p[0].id, sha256_16(p[0].path)] for p in ordered]}
    key = run_key(prompt, master_asset.path, None, params, extra)
    existing = store.find_by_key(key)
    if existing is not None:
        log.warn(f"this round already exists: {existing.dir.name} "
                 f"(stage {existing.stage}) -- reusing it")
        return Prepared(model, limits, geometry, master_plan[1], existing, reused=True)

    run = store.create(key, label=pkg.template, params=params, prompt=prompt)
    log.step(f"round {run.dir.name}")

    prepared_upload: dict[str, tuple[Path, int, int]] = {}
    for (asset, spec, upload_size, needs_scale), info in zip(ordered, refs_params,
                                                             strict=True):
        if needs_scale:
            if asset.role == "mask":
                upload = scale_image(asset.path,
                                     run.dir / f"upload_{asset.id}_{upload_size[0]}x{upload_size[1]}.png",
                                     upload_size, config=conf)
                measured = probe_image(upload, conf)
                recheck = check_image(measured, image_validator)
            elif asset.kind == "video":
                upload = scale_video(
                    asset.path, run.dir / f"upload_{asset.id}_{upload_size[0]}x{upload_size[1]}.mp4",
                    upload_size, config=conf, crf=int(conf.get("mglc.upload_crf", 16)), log=log)
                measured = probe_input(upload, conf)
                recheck = check_input(measured, video_validator)
            else:
                upload = scale_reference_image(
                    asset.path,
                    run.dir / f"upload_{asset.id}_{upload_size[0]}x{upload_size[1]}.png",
                    upload_size, config=conf)
                measured = probe_image(upload, conf)
                recheck = check_image(measured, image_validator)
            if not recheck.ok:
                raise BudgetError(
                    f"素材 {asset.id} 缩放后仍不合约：\n"
                    + "\n".join("  " + c.line() for c in recheck.failures))
            info["upload"] = str(upload)
            info["width"], info["height"] = measured.width, measured.height
            prepared_upload[asset.id] = (Path(upload), measured.width, measured.height)
        else:
            prepared_upload[asset.id] = (Path(asset.path), spec.width, spec.height)

    # 清单现场：断点续跑与人工复查都以它为准（契约 case-model.md §4）。
    (run.dir / "refs.json").write_text(
        json.dumps({"assets": refs_params}, ensure_ascii=False, indent=1),
        encoding="utf-8")

    master_upload = prepared_upload[master_asset.id]
    run.upload = {"path": str(master_upload[0]),
                  "width": master_upload[1], "height": master_upload[2]}
    masks = [a for a in pkg.refs if a.role == "mask"]
    if masks:
        run.mask = {"path": str(prepared_upload[masks[0].id][0])}
    run.params["refs"] = refs_params
    run.save()
    log.info(f"prompt -> {run.prompt_path}")
    return Prepared(model, limits, geometry, master_plan[1], run)


# ---- remote half -----------------------------------------------------------
def execute_round(conf, pkg: InputPackage, prepared: Prepared, *, client: Mglc, log,
                  on_submit=None) -> Outcome:
    """Everything after the local half, each stage skipping itself if already done.

    Re-running after an interruption is the normal path, not an error: the ledger
    says which stages are behind us, and every skip here has a test.
    """
    from ..manual import probe_gate

    run = prepared.run
    outcome = Outcome(run=run, assets=dict(run.assets))
    store = RunStore(pkg.workdir)

    # assets -- needed whenever we are about to submit, which is exactly when a stale
    # recorded id would hurt (the account-switch -400 case).
    if run.task_id is None:
        ledger_refs = run.params.get("refs") or []
        if str(run.params.get("mode")) == "firstLastFrame":
            # CLI 的 --first-image/--last-image 没有 asset-id 参数，首尾图按路径直传。
            log.info("firstLastFrame：首尾图直传，跳过 SD 合规素材登记")
        else:
            items = [(f"{item['kind']}:{item['id']}", item["kind"], item["upload"])
                     for item in ledger_refs]
            outcome.assets = ensure_assets(
                client, run, refs=items,
                timeout=float(conf.get("mglc.review_timeout_s", 1800.0)),
                interval=max(5.0, float(conf.get("mglc.poll_interval_s", 20.0)) / 2),
                log=log)
        run.advance("assets_ready")
        run.save()

    # submit -- the only irreversible, quota-spending call in the package.
    if run.task_id is None:
        outcome.submitted = submit_task(client, run, confirm=True, log=log)
        if on_submit is not None and outcome.submitted is not None:
            on_submit(outcome.submitted)
    elif run.stage == "failed":
        raise LedgerError(f"round {run.dir.name}: task {run.task_id} failed earlier.\n"
                          + failed_round_hint(run))
    else:
        log.info(f"already submitted as task {run.task_id}; continuing")

    if run.result and Path(run.result).is_file():
        outcome.result = Path(run.result)
        log.info(f"result already on disk: {outcome.result}")
    else:
        try:
            wait_task(client, int(run.task_id),
                      interval=float(conf.get("mglc.poll_interval_s", 20.0)),
                      timeout=float(conf.get("mglc.poll_timeout_s", 3600.0)), log=log)
        except TaskFailed as exc:
            run.advance("failed")
            run.note(str(exc))
            run.save()
            raise
        run.advance("succeeded")
        run.save()
        outcome.result = fetch_result(client, run, log=log)

    # checked -- the GENERIC gate (decision D3): decodes, size on record. The
    # geometry gate (band correlation) belongs to vrstudio's recompose.
    if run.gate and run.gate.get("verdict") is not None:
        log.info("gate already recorded for this round")
    else:
        run.gate = probe_gate(Path(outcome.result))
        run.advance("checked")
        run.save()
    outcome.gate = run.gate
    outcome.rounds = store.summary()
    return outcome
