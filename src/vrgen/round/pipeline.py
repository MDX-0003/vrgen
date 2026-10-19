"""The one place the stages are ordered. No CLI vocabulary lives here.

← vrpad aigc/round/pipeline.py 迁入并按 vrgen 契约改写两处：

1. **输入不再来自 case 发现，而是显式输入包**（``InputPackage``）——canvas/source
   尺寸由调用方（vrstudio generate，它手里有 manifest）传入，vrgen 不读任何
   几何契约文件。几何数字进提示词的机制不变（``UploadGeometry`` → 占位符）。
2. **末段闸门换通用闸门**（决策 D3）：vrpad 在这里跑带相关配准（几何闸门），
   那属于 vrstudio 的 recompose；vrgen 只验证"结果能解码、尺寸/帧数在册"。

阶段不变：prepared（本地免费）→ assets（平台状态）→ submitted（花钱、不可逆）
→ succeeded（轮询）→ fetched（结果落盘）→ checked（通用闸门入账）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..errors import AigcError, BudgetError, LedgerError, TaskFailed
from ..paths import templates_dir
from ..platform.client import Mglc
from ..platform.media import probe_input, scale_image, scale_video
from ..platform.models import ModeLimits, ModelInfo, find_model, load_models
from ..prep.budget import InputSpec, check_input
from ..prep.params import effective_aspect
from ..prep.prompt import geometry_variables, render, resolve_template
from ..prep.scale import UploadGeometry, choose_upload_size, parse_size_mode, scaled_geometry
from .assets import ensure_assets
from .ledger import STAGES, Run, RunStore, failed_round_hint, run_key
from .task import fetch as fetch_result
from .task import submit as submit_task
from .task import wait as wait_task

assert STAGES, "stage names live in ledger.py (the lifecycle owner); imported here"


@dataclass(frozen=True)
class InputPackage:
    """一轮生成的全部显式输入（契约：docs/case-model.md §4 的 submit 参数）。

    ``canvas_size``/``source_size`` 是提示词几何占位符的唯一来源——对应 vrpad
    时代从 manifest 读的那两个数（band_w = canvas_w - source_w）。
    """

    job: str
    input: Path
    mask: Path | None
    canvas_size: tuple[int, int]
    source_size: tuple[int, int]
    workdir: Path                      # 台账根（runs/）
    template: str                      # 模板 id（templates/<id>.md）
    variables: dict[str, str]          # prompt-vars 的 [vars]（含本次 --set 合并）


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
    models = load_models(pkg.workdir, url=str(conf.get("mglc.model_list_url")),
                         max_age_h=float(conf.get("mglc.models_max_age_h", 24.0)), log=log)
    model = find_model(models, int(conf.get("mglc.model_id")))
    limits = model.mode(str(conf.get("mglc.mode")))
    aspect = effective_aspect(str(conf.get("mglc.aspect_ratio")), limits.ratios)
    if limits.ratios and aspect not in limits.ratios:
        raise AigcError(
            f"mode {limits.mode} 只接受这些输出比例：{', '.join(limits.ratios)}；"
            f"当前是 {aspect!r}")
    if not limits.takes_video:
        raise AigcError(
            f"model {model.code} ({model.name}) mode {limits.mode!r} does not accept an "
            f"input video; modes that do: {', '.join(model.video_input_modes) or '(none)'}"
            + ("\n  hint: Seedance 2.5 (code 37) with mode videoEdit is the locked edit "
               "path" if model.code != 37 else "")
        )
    validator = limits.ref_video_limits()

    spec = probe_input(pkg.input, conf)
    mode_name, explicit = parse_size_mode(str(conf.get("mglc.scale")))
    upload_size = explicit or choose_upload_size(
        pkg.canvas_size, mode=mode_name, spec=spec,
        max_pixels=int(validator.get("maxPixel") or 0),
        max_bytes=int(validator.get("maxBytes") or 0))
    geometry = scaled_geometry(pkg.canvas_size, pkg.source_size, upload_size)

    template_path = resolve_template(f"{pkg.template}.md", templates_dir())
    if not template_path.is_file():
        raise AigcError(f"prompt template not found: {template_path} "
                        f"(templates/<id>.md)")
    variables: dict[str, object] = dict(geometry_variables(geometry))
    variables.update(pkg.variables)
    prompt = render(template_path.read_text(encoding="utf-8"), variables)

    params = {
        "backend": "mglc",
        "model_id": model.code,
        "mode": limits.mode,
        "resolution": str(conf.get("mglc.resolution")),
        "duration_s": _duration_s(conf, spec),
        "aspect_ratio": aspect,
        "num": int(conf.get("mglc.num", 1)),
        "session_id": int(conf.get("mglc.session_id", 0)),
        "scale": f"{upload_size[0]}x{upload_size[1]}",
        # Not cosmetic: without it the model returns a silent file and recompose,
        # which copies the audio of what came back, delivers a silent panorama.
        "keep_sound": bool(conf.get("mglc.keep_original_sound", True)),
        "input": str(pkg.input),
        "mask": str(pkg.mask) if pkg.mask else None,
        "canvas": f"{pkg.canvas_size[0]}x{pkg.canvas_size[1]}",
        "source": f"{pkg.source_size[0]}x{pkg.source_size[1]}",
        "template": pkg.template,
    }

    store = RunStore(pkg.workdir)
    key = run_key(prompt, pkg.input, pkg.mask, params)
    existing = store.find_by_key(key)
    if existing is not None:
        log.warn(f"this round already exists: {existing.dir.name} "
                 f"(stage {existing.stage}) -- reusing it")
        return Prepared(model, limits, geometry, spec, existing, reused=True)

    run = store.create(key, label=pkg.template, params=params, prompt=prompt)
    log.step(f"round {run.dir.name}")

    if (spec.width, spec.height) != upload_size:
        upload_path = scale_video(
            pkg.input, run.dir / f"upload_{upload_size[0]}x{upload_size[1]}.mp4", upload_size,
            config=conf, crf=int(conf.get("mglc.upload_crf", 16)), log=log)
        measured = probe_input(upload_path, conf)
        recheck = check_input(measured, validator)
        if not recheck.ok:
            raise BudgetError("the rescaled upload still does not fit:\n"
                              + "\n".join("  " + c.line() for c in recheck.failures))
        run.upload = {"path": str(upload_path), "width": measured.width,
                      "height": measured.height}
    else:
        run.upload = {"path": str(pkg.input), "width": spec.width, "height": spec.height}

    if pkg.mask is not None:
        mask_path = Path(pkg.mask)
        if geometry.canvas_size != tuple(pkg.canvas_size):
            mask_path = scale_image(pkg.mask, run.dir / "upload_mask.png",
                                    geometry.canvas_size, config=conf)
        run.mask = {"path": str(mask_path)}
    run.save()
    log.info(f"prompt -> {run.prompt_path}")
    return Prepared(model, limits, geometry, spec, run)


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
        outcome.assets = ensure_assets(
            client, run, video=run.upload.get("path"), mask=(run.mask or {}).get("path"),
            timeout=float(conf.get("mglc.review_timeout_s", 1800.0)),
            interval=max(5.0, float(conf.get("mglc.poll_interval_s", 20.0)) / 2), log=log)
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
