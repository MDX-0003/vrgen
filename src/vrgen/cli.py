"""vrgen CLI——命令契约见 E:\\VRAIGC\\docs\\case-model.md §4。

M1：submit（mglc 全流程，断点续跑）+ login/models/peek。
M3：register/status/result（manual 后端）。
退出码：0 成功 / 2 参数或配置错（UsageError）/ 1 运行失败（VrgenError，台账有现场）。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import typer

from . import __version__, jobs, manual
from . import config as config_mod
from . import refs as refs_mod
from . import sessions as sessions_mod
from .errors import TaskFailed, UsageError, VrgenError
from .paths import templates_dir
from .platform.client import Mglc
from .platform.media import probe_image, probe_input
from .platform.models import dropdown_models, find_model, load_image_models, load_models
from .prep import params as params_mod
from .prep import prompt as prompt_mod
from .progress import Log
from .round import image as image_mod
from .round import kinds as kinds_mod
from .round.ledger import PROGRESS_LOG, RunStore, append_call_log, resubmit_token, shell_line
from .round.pipeline import InputPackage, execute_round, prepare_round
from .round.task import build_generate_argv
from .round.task import state as task_state

app = typer.Typer(
    help="视频生成服务：mglc(Seedance) 与手动注册双后端，统一轮次台账。",
    no_args_is_help=True,
    invoke_without_command=True,
)


def _fail(exc: VrgenError) -> None:
    code = 2 if isinstance(exc, UsageError) else 1
    typer.secho(f"vrgen: {exc}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code) from exc


@app.callback()
def _root(
    version: bool = typer.Option(False, "--version", help="打印版本并退出。"),
) -> None:
    if version:
        typer.echo(f"vrgen {__version__}")
        raise typer.Exit()


# ── mglc 后端：submit ────────────────────────────────────────────────────


def _apply_generation_params(conf, workdir, job, *, video_model_id, mode, resolution,
                             duration, num, aspect_ratio, scale, session_id=None,
                             audio=None) -> None:
    """Resolve submit parameters: CLI > generation.toml > config.

    Every option except ``scale`` and ``audio`` maps one-to-one onto a ``mglc
    video generate`` flag (``--mode`` is mglc ``--type``; ``--video-model-id`` is
    mglc ``--model-id``). ``scale`` is vrgen's own upload decision and has no mglc
    counterpart; it changes the prompt geometry. ``audio=none`` is also vrgen's
    own: the platform has no "do not generate sound" flag, so the track is
    stripped after download. ``session_id`` selects a project
    generation session, so the spend is billed to that project's balance instead
    of the personal one.
    """
    saved = jobs.load_generation_params(workdir, job)
    picks = {
        "video_model_id": (video_model_id if video_model_id is not None
                           else saved.video_model_id),
        "mode": mode if mode is not None else saved.mode,
        "resolution": resolution if resolution is not None else saved.resolution,
        "duration_s": duration,
        "num": num,
        "aspect_ratio": (aspect_ratio if aspect_ratio is not None
                         else saved.aspect_ratio),
        "scale": scale if scale is not None else saved.scale,
        "session_id": session_id if session_id is not None else saved.session_id,
        "audio": audio,
    }
    for key, value in picks.items():
        if value is not None:
            conf.data["mglc"][key] = value

    if int(conf.get("mglc.video_model_id")) < 1:
        raise UsageError("--video-model-id 必须是平台模型 ID（fullredraw 用 37）")
    if not str(conf.get("mglc.mode")).strip():
        raise UsageError("--mode 不能为空（fullredraw 用 videoEdit）")
    # CLI/草稿只接受页面上的档位；config 里仍可保留 quarter/WxH 供内部调试。
    if resolution is not None and resolution not in params_mod.OUTPUT_TIERS:
        raise UsageError("--resolution 只能是 480p | 720p | 1080p | 4K")
    if saved.resolution is not None and saved.resolution not in params_mod.OUTPUT_TIERS:
        raise UsageError(f"generation.toml: resolution {saved.resolution!r} 不合法")
    duration_s = int(conf.get("mglc.duration_s"))
    if duration_s < 0 or 0 < duration_s < 4:
        raise UsageError("--duration 必须 >= 4（0 表示按输入时长自动取整）")
    if not 1 <= int(conf.get("mglc.num")) <= 4:
        raise UsageError("--num 必须在 1..4")
    if not str(conf.get("mglc.aspect_ratio")).strip():
        raise UsageError("--aspect-ratio 不能为空（写 W:H 如 21:9；videoEdit 跟随输入时写 auto）")
    aspect = str(conf.get("mglc.aspect_ratio")).strip()
    if aspect != "auto":
        left, _, right = aspect.partition(":")
        if not (left.isdigit() and right.isdigit() and int(left) > 0 and int(right) > 0):
            raise UsageError("--aspect-ratio 必须是 W:H（如 21:9），或 auto 表示跟随输入")
    if scale is not None and scale not in params_mod.INPUT_MODES:
        raise UsageError("--scale 只能是 auto | full | half")
    if audio is not None and audio not in ("keep", "none"):
        raise UsageError("--audio 只能是 keep | none")
    if saved.scale is not None and saved.scale not in params_mod.INPUT_MODES:
        raise UsageError(f"generation.toml: scale {saved.scale!r} 不合法")
    if int(conf.get("mglc.session_id", 0)) < 0:
        raise UsageError("--session-id 必须 >= 0（0 = 个人额度）")


def _image_models_payload(conf, workdir, job) -> tuple[list[dict], str]:
    """图片模型清单（mglc model list --type image，24h 缓存）。

    取不到不炸整个 params show：视频侧字段照常返回，图片侧给空清单加
    一句原因，页面据此在下拉框处显示提示。
    """
    from .platform.binpath import find_mglc
    from .platform.client import Mglc
    client = Mglc(config=conf, binary=find_mglc(conf) or "mglc")

    def fetch() -> list[dict]:
        data = client.model_list("image", page_size=100).data
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise VrgenError(f"mglc model list --type image 返回形状异常：{str(data)[:120]}")
        return items

    try:
        entries = load_image_models(
            jobs.job_dir(workdir, job), fetcher=fetch,
            max_age_h=float(conf.get("mglc.models_max_age_h", 24.0)),
            log=Log(quiet=True))
        return [{"id": m.code, "name": m.name, "description": m.description}
                for m in entries], ""
    except Exception as exc:                    # mglc 缺席/未登录/网络失败都算
        return [], f"图片模型清单暂时读不了：{exc}"


def _parse_kv(items: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items or []:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise UsageError(f"--set 需要 key=value，got {item!r}")
        out[key.strip()] = value
    return out


def _image_role_args(roles: list[str] | None,
                     notes: list[str] | None) -> tuple[list[str], list[str]]:
    """校验 ``--image-role`` 与 ``--image-note`` 的配对。

    note 可以整组省略；一旦提供，条数就必须与 role 相同，空字符串表示这张图
    没有额外用途。role 与 image 的条数由调用方各自校验。
    """
    role_list = list(roles or ())
    note_list = list(notes or ())
    if note_list and len(note_list) != len(role_list):
        raise UsageError("--image-note 的条数必须与 --image-role 相同")
    return role_list, note_list


def _parse_wh(text: str, what: str) -> tuple[int, int]:
    left, _, right = str(text).lower().partition("x")
    try:
        w, h = int(left), int(right)
    except ValueError as exc:
        raise UsageError(f"{what} {text!r} 不是 WxH（如 4668x2000）") from exc
    if w < 2 or h < 2:
        raise UsageError(f"{what} {text!r}：两边都要 >= 2")
    return w, h


@app.command()
def submit(
    job: str = typer.Option(..., "--job", help="job 名（调用方通常传 case 名）。"),
    refs: Path = typer.Option(..., "--refs", exists=True, dir_okay=False,
                              help="提交资产清单 JSON（三流程唯一的提交入口）。"),
    canvas: str = typer.Option(None, "--canvas",
                               help="画布尺寸 WxH；省略时由母版推导。"),
    source: str = typer.Option(None, "--source",
                               help="源尺寸 WxH；省略时等于画布。"),
    template: str = typer.Option(
        None, "--template", help="模板 id；首次指定后记入 prompt-vars 草稿。"),
    sets: list[str] = typer.Option(None, "--set",
                                    help="提示词变量 k=v（自适应于草稿 [vars]）。"),
    video_model_id: int = typer.Option(None, "--video-model-id",
                                 help="← mglc --model-id；fullredraw 固定 37。"),
    mode: str = typer.Option(None, "--mode",
                             help="← mglc --type；fullredraw 固定 videoEdit。"),
    resolution: str = typer.Option(None, "--resolution",
                                    help="← mglc --resolution；输出档位 480p|720p|1080p|4K。"),
    duration: int = typer.Option(None, "--duration",
                                 help="← mglc --duration；整数秒，默认按输入时长取整。"),
    num: int = typer.Option(None, "--num",
                            help="← mglc --num；1..4（当前只取第一个结果）。"),
    aspect_ratio: str = typer.Option(None, "--aspect-ratio",
                                      help="← mglc --aspect-ratio；fullredraw 固定 21:9。"),
    scale: str = typer.Option(None, "--scale",
                              help="vrgen 自有上传缩放：auto|full|half（无 mglc 对应 flag）。"),
    session_id: int = typer.Option(
        None, "--session-id",
        help="项目生成会话 ID（>0）：在该项目的额度下提交；0 = 个人额度。"),
    audio: str = typer.Option(
        None, "--audio",
        help="keep | none：none 时下载结果后去掉音轨（生成阶段平台无法关闭声音）。"),
    dry_run: bool = typer.Option(False, "--dry-run",
                                  help="只做本地半场（缩档+提示词+台账），不发请求不花钱。"),
    resubmit: bool = typer.Option(
        False, "--resubmit",
        help="不再复用已有轮次：同提示词另开一轮并重新计费（多抽几版时用）。"),
    workdir: Path = typer.Option(None, "--workdir",
                                 help="台账根（默认 $VRGEN_RUNS_DIR）。"),
    config: Path = typer.Option(None, "--config", help="TOML 配置文件。"),
) -> None:
    """提交一轮生成：本地半场 → 素材登记 → 提交 → 轮询 → 下载 → 通用闸门。"""
    log = Log()
    try:
        conf = config_mod.load(config)
        _apply_generation_params(
            conf, workdir, job, video_model_id=video_model_id, mode=mode,
            resolution=resolution, duration=duration, num=num,
            aspect_ratio=aspect_ratio, scale=scale, session_id=session_id,
            audio=audio,
        )
        if (canvas is None) != (source is None):
            raise UsageError("--canvas 与 --source 要么都给，要么都不给")
        canvas_size = _parse_wh(canvas, "--canvas") if canvas else None
        source_size = _parse_wh(source, "--source") if source else None
        ref_assets = refs_mod.load_refs(refs)
        refs_mod.validate_mode(ref_assets, str(conf.get("mglc.mode")))

        # prompt-vars 草稿 + --set（自适应校验）+ 模板解析（首次指定记入草稿）
        pv = jobs.load_prompt_vars(workdir, job)
        overrides = _parse_kv(sets)
        merged = jobs.apply_set(pv, overrides) if overrides else pv
        template_id = template or pv.template or str(conf.get("mglc.prompt_template"))
        if (template_id != pv.template
                and (template is not None or (ref_assets and pv.template is None))):
            jobs.save_prompt_vars(workdir, job,
                                  jobs.PromptVars(template=template_id, vars=pv.vars))
        if not (Path(templates_dir()) / f"{template_id}.md").is_file():
            available = sorted(p.stem for p in Path(templates_dir()).glob("*.md"))
            raise UsageError(f"模板 {template_id!r} 不存在；可用：{available}")

        pkg = InputPackage(job=job, workdir=jobs.job_dir(workdir, job),
                           template=template_id, variables=dict(merged.vars),
                           refs=ref_assets, canvas_size=canvas_size,
                           source_size=source_size)
        prepared = prepare_round(conf, pkg, log=log,
                                 resubmit_token=resubmit_token() if resubmit else None)
    except VrgenError as exc:
        _fail(exc)

    # 从这里起每行同时落轮次目录：分钟级等待只有终端回滚不是记录。
    log.attach(prepared.run.dir / PROGRESS_LOG)

    print(f"job         {job}")
    print(f"round       {prepared.run.dir}")
    if prepared.reused:
        print(f"reused      {prepared.run.dir.name}  reason=key  "
              f"task={prepared.run.task_id or '-'}  (本次未提交，未花费)")
    print(f"upload      {prepared.run.upload.get('path')}")
    print(f"            {prepared.geometry.describe()}")
    print(f"mask        {(prepared.run.mask or {}).get('path') or '(none)'}")
    print(f"model       {prepared.model.code} {prepared.model.name}  "
          f"mode {prepared.limits.mode}  {prepared.run.params['resolution']}  "
          f"{prepared.run.params['duration_s']}s  x{prepared.run.params['num']}")
    print(f"prompt      {prepared.run.prompt_path}\n")
    print(prepared.run.prompt)

    from .platform.binpath import find_mglc
    common = dict(
        prompt=prepared.run.prompt,
        model_id=int(prepared.run.params["video_model_id"]),
        mode=str(prepared.run.params["mode"]),
        duration_s=int(prepared.run.params["duration_s"]),
        resolution=str(prepared.run.params["resolution"]),
        aspect_ratio=str(prepared.run.params["aspect_ratio"]),
        num=int(prepared.run.params.get("num", 1)),
        keep_sound=bool(prepared.run.params.get("keep_sound", True)),
        session_id=int(prepared.run.params.get("session_id", 0) or 0),
        preview=True)
    ledger_refs = prepared.run.params.get("refs") or []
    if str(prepared.run.params.get("mode")) == "firstLastFrame":
        by_role = {str(item.get("role")): item for item in ledger_refs}
        first = by_role.get("first_frame")
        last = by_role.get("last_frame")
        if first is None or last is None:
            raise UsageError("firstLastFrame 台账缺 first_frame 或 last_frame")
        gen_argv = build_generate_argv(
            **common,
            first_image=(str(first["upload"]), None),
            last_image=(str(last["upload"]), None))
    else:
        videos = [(str(item["upload"]),
                   prepared.run.assets.get(f"video:{item['id']}"))
                  for item in ledger_refs if item.get("kind") == "video"]
        # mask 固定追加在所有图片之后（装配器已经排在最后，这里再保证一次）。
        images = [(str(item["upload"]),
                   prepared.run.assets.get(f"image:{item['id']}"))
                  for item in ledger_refs
                  if item.get("kind") == "image" and item.get("role") != "mask"]
        images += [(str(item["upload"]), prepared.run.assets.get(f"image:{item['id']}"))
                   for item in ledger_refs if item.get("role") == "mask"]
        gen_argv = build_generate_argv(**common, videos=videos, images=images)
    command = shell_line([find_mglc(conf) or "mglc", "video", "generate", *gen_argv])
    print(f"\ncommand     {command}")

    if dry_run:
        print("\n--dry-run：什么都没发，没花额度。")
        if str(prepared.run.params.get("audio", "keep")) == "none":
            print("audio       none（结果下载后去音轨；平台侧仍会生成声音）")
        _rounds_line(job, workdir)
        print(f"calls       {append_call_log(prepared.run, [], mode='vrgen submit --dry-run')}")
        return

    _wait_banner(conf, prepared)
    if prepared.run.task_id is not None:
        _announce_task(prepared.run.task_id, resumed=True, run=prepared.run)
    client = Mglc(config=conf, binary=find_mglc(conf))
    try:
        outcome = execute_round(conf, pkg, prepared, client=client, log=log,
                                on_submit=_announce_task)
    except TaskFailed as exc:
        log.error(str(exc))
        print(f"    重试同一提示词：把 {prepared.run.dir} 移走再跑一次\n"
              f"    （旧任务可能已计费，所以不自动重发）")
        raise typer.Exit(1) from exc
    except VrgenError as exc:
        log.error(str(exc))
        raise typer.Exit(1) from exc
    finally:
        print(f"calls       "
              f"{append_call_log(prepared.run, client.transcript, mode='vrgen submit')}")
        log.close()

    result = outcome.result
    print(f"\nresult      {result}  ({Path(result).stat().st_size / 1e6:.1f} MB)")
    gate = outcome.gate or {}
    print(f"gate        {gate.get('verdict')}  "
          f"{gate.get('width')}x{gate.get('height')}  "
          f"{gate.get('frames', '?')} frames  ({gate.get('duration_s', '?')}s)")
    if gate.get("verdict") == "error":
        raise typer.Exit(1)
    _rounds_line(job, workdir)
    print(f"next        vrstudio generate adopt --case {job}   "
          f"(把这一轮结果拷回 case 并打印 recompose 命令)")


def _announce_task(task_id: int, *, resumed: bool = False, run=None) -> None:
    """task id 一到手就交出去——等待以分钟计，用户该能在另一个终端瞟一眼。"""
    if resumed:
        where = run.dir.name if run is not None else "?"
        print(f"reused      {where}  reason=resumed  task={task_id}  (本次未提交，未花费)")
    tail = "  (已提交过；继续等)" if resumed else ""
    print(f"task        {task_id}{tail}")
    print(f"peek        vrgen peek {task_id}")


def _wait_banner(conf, prepared) -> None:
    estimate = prepared.model.estimated_seconds
    minutes = f"~{estimate / 60:.0f} min" if estimate else "several minutes"
    if prepared.run.task_id is not None and prepared.run.result:
        return
    print("\n-> platform  上传 + 审核，提交，然后等渲染")
    print(f"   {prepared.model.name} 自报 {minutes} 每次渲染；"
          f"每 {conf.get('mglc.poll_interval_s'):g}s 轮询，"
          f"{conf.get('mglc.poll_timeout_s'):g}s 后放弃")
    print("   Ctrl-C 任意时刻安全：台账记着 task id，重跑这条命令从断点继续、不重复花钱。")


def _rounds_line(job: str, workdir) -> None:
    rows = RunStore(jobs.job_dir(workdir, job)).summary()
    if rows:
        print(f"rounds      {len(rows)} recorded ({', '.join(rows)})")


# ── 图片生成：关键帧工作台（契约 case-model.md §4）──────────────────────

image_app = typer.Typer(
    help="图片生成任务：提交、轮询、下载全部候选；台账记为 img- 轮次。",
    no_args_is_help=True,
)
app.add_typer(image_app, name="image")


@image_app.command("submit")
def image_submit(
    job: str = typer.Option(..., "--job", help="job 名（调用方通常传 case 名）。"),
    prompt: str = typer.Option(None, "--prompt", help="图片提示词正文；与 --template 二选一。"),
    template: str = typer.Option(None, "--template",
                                 help="提示词模板 id；由 vrgen 用草稿变量展开。"),
    vars_file: str = typer.Option(jobs.PROMPT_VARS, "--vars-file",
                                  help="模板变量草稿文件名（默认 prompt-vars.toml）。"),
    images: list[Path] = typer.Option(None, "--image", exists=True, dir_okay=False,
                                      help="参考图，可重复；按顺序传给 mglc。"),
    image_roles: list[str] = typer.Option(
        None, "--image-role", help="参考图角色，可重复；条数与 --image 相同，顺序对应。"),
    image_notes: list[str] = typer.Option(
        None, "--image-note",
        help="参考图用途，可重复；条数与 --image 相同，空字符串表示没有用途。"),
    step: str = typer.Option("", "--step",
                             help="首尾帧工作台步骤：first | last；只写台账，不发给平台。"),
    image_model_id: int = typer.Option(
        None, "--image-model-id",
        help="图片模型 ID；缺省取 generation.toml 的 image_model_id，再缺省取 config。"),
    ratio: str = typer.Option(None, "--ratio", help="输出比例；缺省取 config 的 image_ratio。"),
    resolution: str = typer.Option(
        None, "--resolution", help="1K | 2K | 4K；缺省取 config 的最高档。"),
    num: int = typer.Option(None, "--num", help="候选数量 1..4；缺省取 config。"),
    session_id: int = typer.Option(
        None, "--session-id", help="项目生成会话 ID；0 = 个人额度。"),
    canvas: str = typer.Option(
        None, "--canvas", metavar="WxH",
        help="画布尺寸（360 静图用）：按 21:9 画布与源宽推出冗余带列号占位符。"),
    source: str = typer.Option(
        None, "--source", metavar="WxH",
        help="源图尺寸（360 静图用）：与 --canvas 一起给，缺一不可。"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只建台账并打印 argv，不发请求。"),
    resubmit: bool = typer.Option(
        False, "--resubmit",
        help="不再复用已有轮次：同提示词另开一轮并重新计费（多抽几版时用）。"),
    workdir: Path = typer.Option(None, "--workdir", help="台账根（默认 $VRGEN_RUNS_DIR）。"),
    config: Path = typer.Option(None, "--config", help="TOML 配置文件。"),
) -> None:
    """提交一轮图片生成：提交 → 轮询 → 下载全部候选 → 逐张闸门。"""
    log = Log()
    try:
        conf = config_mod.load(config)
        if bool(prompt) == bool(template):
            raise UsageError("--prompt 与 --template 必须二选一")
        if step not in ("", "first", "last"):
            raise UsageError(f"--step 只能是 first 或 last；当前是 {step!r}")
        if image_roles and not template:
            raise UsageError("--image-role 只在 --template 时使用")
        if image_notes and not template:
            raise UsageError("--image-note 只在 --template 时使用")
        if template and len(image_roles or []) != len(images or []):
            raise UsageError("--image-role 的条数必须与 --image 相同")
        if image_notes and len(image_notes) != len(images or []):
            raise UsageError("--image-note 的条数必须与 --image 相同")
        roles, notes = _image_role_args(image_roles, image_notes)
        resolved_prompt = prompt
        if template:
            extra = {"image_roles": image_mod.image_roles_text(roles, notes)}
            if bool(canvas) != bool(source):
                raise UsageError("--canvas 与 --source 必须成对给出")
            if canvas:
                extra.update(prompt_mod.geometry_from_sizes(
                    _parse_wh(canvas, "--canvas"), _parse_wh(source, "--source")))
            resolved_prompt = jobs.render_template(workdir, job, template,
                                                   vars_file=vars_file, extra=extra)
        saved = jobs.load_generation_params(workdir, job)
        resolved_model = int(
            image_model_id if image_model_id is not None
            else (saved.image_model_id
                  if saved.image_model_id is not None
                  else conf.get("mglc.image_model_id", 44)))
        resolved_ratio = str(ratio or conf.get("mglc.image_ratio", "16:9"))
        resolved_resolution = str(resolution or conf.get("mglc.image_resolution", "4K"))
        resolved_num = int(num if num is not None else conf.get("mglc.image_num", 4))
        resolved_session = int(session_id if session_id is not None
                               else conf.get("mglc.session_id", 0) or 0)
        pkg = image_mod.ImagePackage(
            job=job, prompt=resolved_prompt, model_id=resolved_model,
            images=tuple(images or ()), ratio=resolved_ratio,
            resolution=resolved_resolution, num=resolved_num,
            workdir=jobs.job_dir(workdir, job), session_id=resolved_session,
            step=step)
        prepared = kinds_mod.KINDS["image"].prepare_round(
            conf, pkg, log=log,
            resubmit_token=resubmit_token() if resubmit else None)
    except VrgenError as exc:
        _fail(exc)

    log.attach(prepared.run.dir / PROGRESS_LOG)
    print(f"job         {job}")
    print(f"round       {prepared.run.dir}")
    if prepared.reused:
        print(f"reused      {prepared.run.dir.name}  reason=key  "
              f"task={prepared.run.task_id or '-'}  (本次未提交，未花费)")
    elif prepared.run.task_id is not None:
        print(f"reused      {prepared.run.dir.name}  reason=resumed  "
              f"task={prepared.run.task_id}  (本次未提交，未花费)")
    print(f"model       {resolved_model}  {resolved_ratio}  {resolved_resolution}  "
          f"x{resolved_num}")
    print(f"images      {', '.join(str(p) for p in pkg.images)}")
    print(f"prompt      {prepared.run.prompt_path}\n")
    print(prepared.run.prompt)

    argv = kinds_mod.KINDS["image"].build_argv(
        prompt=pkg.prompt, model_id=pkg.model_id, images=list(pkg.images),
        ratio=pkg.ratio, resolution=pkg.resolution, num=pkg.num,
        session_id=pkg.session_id)
    from .platform.binpath import find_mglc
    command = shell_line([find_mglc(conf) or "mglc",
                          *kinds_mod.KINDS["image"].quota_command, *argv])
    print(f"\ncommand     {command}")

    if dry_run:
        print("\n--dry-run：什么都没发，没花额度。")
        print(f"calls       "
              f"{append_call_log(prepared.run, [], mode='vrgen image submit --dry-run')}")
        return

    client = Mglc(config=conf, binary=find_mglc(conf))
    try:
        outcome = kinds_mod.KINDS["image"].execute_round(
            conf, pkg, prepared, client=client, log=log)
    except UsageError as exc:
        _fail(exc)
    except TaskFailed as exc:
        log.error(str(exc))
        raise typer.Exit(1) from exc
    except VrgenError as exc:
        log.error(str(exc))
        raise typer.Exit(1) from exc
    finally:
        print(f"calls       "
              f"{append_call_log(prepared.run, client.transcript, mode='vrgen image submit')}")
        log.close()

    for index, path in enumerate(outcome.results, start=1):
        print(f"result[{index}]   {path}  "
              f"({Path(path).stat().st_size / 1e6:.1f} MB)")
    gate = outcome.gate or {}
    print(f"gate        {gate.get('verdict')}  {len(outcome.results)} candidate(s)")
    if gate.get("verdict") == "error":
        raise typer.Exit(1)


# ── 平台辅助：login / models / peek ─────────────────────────────────────


@app.command()
def login() -> None:
    """登录态管理（交互式透传 mglc login）。"""
    from .platform.binpath import find_mglc

    conf = config_mod.load()
    binary = find_mglc(conf) or "mglc"
    raise typer.Exit(subprocess.run([binary, "login"]).returncode)


@app.command()
def models(
    refresh: bool = typer.Option(False, "--refresh", help="强制刷新清单缓存。"),
    workdir: Path = typer.Option(None, "--workdir", help="缓存位置（台账根）。"),
) -> None:
    """平台模型清单（24h 缓存）。"""
    from .platform.models import load_models

    log = Log()
    root = jobs.resolve_workdir(workdir)
    try:
        items = load_models(root, refresh=refresh,
                            url=str(config_mod.load().get("mglc.model_list_url")), log=log)
    except VrgenError as exc:
        _fail(exc)
    for m in items:
        modes = ",".join(sorted(m.modes))
        video = ",".join(m.video_input_modes) or "(none)"
        print(f"  {m.code:<6} {m.name:<28} modes: {modes}   video-input: {video}")


@app.command()
def peek(task_id: int = typer.Argument(..., help="平台任务 id（零额度查询）。")) -> None:
    """零额度查平台侧任务状态。"""
    from .platform.binpath import find_mglc
    from .round.task import TASK_STATES

    try:
        conf = config_mod.load()
        client = Mglc(config=conf, binary=find_mglc(conf))
        code, text = task_state(client, task_id)
    except VrgenError as exc:
        _fail(exc)
    print(f"task {task_id}: code={code} state={text!r} "
          f"(known: {TASK_STATES.get(code, code)})")


# ── 项目/剧集/生成会话（只读，web 会话选择器的数据源）────────────────────

sessions_app = typer.Typer(
    help="项目/剧集/生成会话：只读查询，JSON 输出（契约 case-model.md §4）。",
    no_args_is_help=True,
)
app.add_typer(sessions_app, name="sessions")


@sessions_app.command("list")
def sessions_list(
    project_id: int = typer.Option(None, "--project-id", help="只展开这个项目。"),
    episode: int = typer.Option(None, "--episode",
                                help="只展开这一集；必须与 --project-id 一起给。"),
    config: Path = typer.Option(None, "--config", help="TOML 配置文件。"),
) -> None:
    """列出项目/剧集/生成会话树（只读命令，不消耗额度）。"""
    from .platform.binpath import find_mglc

    try:
        conf = config_mod.load(config)
        client = Mglc(config=conf, binary=find_mglc(conf))
        payload = sessions_mod.tree(client, project_id=project_id, episode=episode)
    except VrgenError as exc:
        _fail(exc)
    _echo_payload(payload)


# ── 提示词模板与变量草稿（web 提示词编辑界面的数据源，决策 18）────────────

prompt_app = typer.Typer(
    help="提示词模板与变量草稿：show 读、save 写；JSON 输出（契约 case-model.md §4）。",
    no_args_is_help=True,
)
app.add_typer(prompt_app, name="prompt")


def _prompt_default() -> str:
    return str(config_mod.load().get("mglc.prompt_template") or "")


def _echo_payload(payload: dict) -> None:
    # ensure_ascii：输出纯 ASCII，Windows 控制台/管道编码差异不再可能咬人
    typer.echo(json.dumps(payload, ensure_ascii=True, indent=1))


def _prompt_preview(payload: dict, *, workdir, job: str,
                    refs: Path | None, mode: str | None,
                    canvas: str | None, source: str | None,
                    image_roles: list[str] | None = None,
                    image_notes: list[str] | None = None) -> dict:
    """给 prompt show/save 的返回补上展开后的正文（rendered / render_error）。

    预览只读：不建轮次、不碰平台。素材清单需要 ``--refs``；模式省略时读
    generation.toml；画布与源尺寸需要成对给出。
    """
    text = payload.get("template_text")
    if not text:
        return payload
    variables: dict[str, object] = dict(payload.get("vars") or {})
    try:
        if refs is not None:
            loaded = refs_mod.load_refs(refs)
            variables["asset_list"] = refs_mod.asset_list(loaded)
            variables.update(refs_mod.mask_variables(loaded))
        resolved_mode = mode
        if resolved_mode is None:
            resolved_mode = jobs.load_generation_params(workdir, job).mode
        if resolved_mode:
            variables.update(refs_mod.mode_variables(resolved_mode))
        if canvas is not None and source is not None:
            variables.update(prompt_mod.geometry_from_sizes(
                _parse_wh(canvas, "--canvas"), _parse_wh(source, "--source")))
        if image_roles:
            variables["image_roles"] = image_mod.image_roles_text(image_roles, image_notes)
        payload["rendered"] = prompt_mod.render(text, variables)
        payload["render_error"] = None
    except (VrgenError, OSError, ValueError) as exc:
        payload["rendered"] = None
        payload["render_error"] = str(exc)
    return payload


@prompt_app.command("show")
def prompt_show(
    job: str = typer.Option(..., "--job", help="job 名（调用方通常传 case 名）。"),
    template: str = typer.Option(None, "--template", help="覆盖草稿/配置的模板选择。"),
    vars_file: str = typer.Option(jobs.PROMPT_VARS, "--vars-file",
                                  help="变量草稿文件名（默认 prompt-vars.toml）。"),
    workdir: Path = typer.Option(None, "--workdir", help="台账根（默认 $VRGEN_RUNS_DIR）。"),
    refs: Path = typer.Option(None, "--refs", exists=True, dir_okay=False,
                              help="gen 素材清单 JSON；提供后展开素材清单。"),
    mode: str = typer.Option(None, "--mode",
                             help="生成模式；省略时读 generation.toml。"),
    image_roles: list[str] = typer.Option(
        None, "--image-role", help="参考图角色，可重复；与 --image 顺序对应，展开 image_roles。"),
    image_notes: list[str] = typer.Option(
        None, "--image-note", help="参考图用途，可重复；与 --image-role 顺序对应。"),
    canvas: str = typer.Option(None, "--canvas", help="画布尺寸 WxH；展开几何占位用。"),
    source: str = typer.Option(None, "--source", help="源尺寸 WxH；展开几何占位用。"),
) -> None:
    """读模板与变量草稿：正文、占位符、几何键、[vars] 当前值。"""
    try:
        payload = jobs.prompt_overview(workdir, job, template=template,
                                       config_default=_prompt_default(),
                                       vars_file=vars_file)
    except VrgenError as exc:
        _fail(exc)
    try:
        roles, notes = _image_role_args(image_roles, image_notes)
    except VrgenError as exc:
        _fail(exc)
    payload = _prompt_preview(payload, workdir=workdir, job=job, refs=refs,
                              mode=mode, canvas=canvas, source=source,
                              image_roles=roles, image_notes=notes)
    _echo_payload(payload)


@prompt_app.command("save")
def prompt_save(
    job: str = typer.Option(..., "--job", help="job 名（调用方通常传 case 名）。"),
    sets: list[str] = typer.Option(None, "--set",
                                   help="新增或覆盖变量 k=v（允许新键，直接写入草稿）。"),
    unsets: list[str] = typer.Option(None, "--unset",
                                     help="删除变量；模板正文引用的键拒绝删除。"),
    template: str = typer.Option(None, "--template", help="切换模板 id（必须存在）。"),
    vars_file: str = typer.Option(jobs.PROMPT_VARS, "--vars-file",
                                  help="变量草稿文件名（默认 prompt-vars.toml）。"),
    workdir: Path = typer.Option(None, "--workdir", help="台账根（默认 $VRGEN_RUNS_DIR）。"),
    refs: Path = typer.Option(None, "--refs", exists=True, dir_okay=False,
                              help="gen 素材清单 JSON；提供后展开素材清单。"),
    mode: str = typer.Option(None, "--mode",
                             help="生成模式；省略时读 generation.toml。"),
    image_roles: list[str] = typer.Option(
        None, "--image-role", help="参考图角色，可重复；与 --image 顺序对应，展开 image_roles。"),
    image_notes: list[str] = typer.Option(
        None, "--image-note", help="参考图用途，可重复；与 --image-role 顺序对应。"),
    canvas: str = typer.Option(None, "--canvas", help="画布尺寸 WxH；展开几何占位用。"),
    source: str = typer.Option(None, "--source", help="源尺寸 WxH；展开几何占位用。"),
) -> None:
    """编辑变量草稿并写回指定的 .toml（JSON 回显保存后的状态）。"""
    try:
        payload = jobs.edit_prompt_vars(workdir, job, sets=_parse_kv(sets),
                                        unsets=unsets, template=template,
                                        config_default=_prompt_default(),
                                        vars_file=vars_file)
    except VrgenError as exc:
        _fail(exc)
    try:
        roles, notes = _image_role_args(image_roles, image_notes)
    except VrgenError as exc:
        _fail(exc)
    payload = _prompt_preview(payload, workdir=workdir, job=job, refs=refs,
                              mode=mode, canvas=canvas, source=source,
                              image_roles=roles, image_notes=notes)
    _echo_payload(payload)


# ── 生成参数草稿（web 生成页的数据源，决策 20）─────────────────────────

params_app = typer.Typer(
    help="生成参数草稿：show 读输出/输入分辨率选项，save 写 generation.toml；JSON 输出。",
    no_args_is_help=True,
)
app.add_typer(params_app, name="params")


@params_app.command("show")
def params_show(
    job: str = typer.Option(..., "--job", help="job 名（调用方通常传 case 名）。"),
    refs: Path = typer.Option(None, "--refs", exists=True, dir_okay=False,
                              help="提交资产清单 JSON；省略时只返回模式与当前草稿。"),
    canvas: str = typer.Option(None, "--canvas", help="画布尺寸 WxH；省略时由母版推导。"),
    source: str = typer.Option(None, "--source", help="源尺寸 WxH；省略时等于画布。"),
    aspect_ratio: str = typer.Option(
        None, "--aspect-ratio",
        help="覆盖 fixed 里的输出比例；auto 表示跟随输入（gen 流用）。"),
    config: Path = typer.Option(None, "--config", help="TOML 配置文件。"),
    workdir: Path = typer.Option(None, "--workdir", help="台账根（默认 $VRGEN_RUNS_DIR）。"),
) -> None:
    """输出分辨率档位 + 输入缩放档位的实际宽高，以及当前草稿/固定参数。

    没有 ``--refs`` 时（新建 gen case、素材还没加）只返回 ``modes`` 与
    ``current``，``output``/``input`` 为空数组；页面据此先渲染模式下拉框。
    输入档位只对视频母版有意义：母版是图片时 ``input`` 为空。
    """
    try:
        conf = config_mod.load(config)
        _apply_generation_params(
            conf, workdir, job, video_model_id=None, mode=None, resolution=None,
            duration=None, num=None, aspect_ratio=aspect_ratio, scale=None,
        )
        models = load_models(jobs.job_dir(workdir, job),
                             url=str(conf.get("mglc.model_list_url")),
                             max_age_h=float(conf.get("mglc.models_max_age_h", 24.0)),
                             log=Log(quiet=True))
        model = find_model(models, int(conf.get("mglc.video_model_id")))
        limits = model.mode(str(conf.get("mglc.mode")))
        saved = jobs.load_generation_params(workdir, job)
        image_model_id = int(
            saved.image_model_id
            if saved.image_model_id is not None
            else conf.get("mglc.image_model_id", 44))
        image_models, image_models_error = _image_models_payload(conf, workdir, job)
        aspect = params_mod.effective_aspect(str(conf.get("mglc.aspect_ratio")),
                                             limits.ratios)
        modes = [
            {
                "value": m.mode,
                "label": params_mod.MODE_LABELS.get(m.mode, m.mode),
                "takes_video": m.takes_video,
                "ratios": list(m.ratios),
                "max_count": m.max_count,
                "max_videos": (getattr(m, "max_videos", 0) if m.takes_video else 0),
                "max_images": getattr(m, "max_images", 0),
                "video_required": m.mode in ("videoEdit", "videoExtend"),
                "duration": dict(m.duration or {}),
            }
            for m in model.modes.values()
            if m.mode in params_mod.SUPPORTED_MODES
        ]
        common = {
            "job": job,
            "modes": modes,
            "models": dropdown_models(models),
            "image_models": image_models,
            **({"image_models_error": image_models_error}
               if image_models_error else {}),
            "current": {
                "resolution": str(conf.get("mglc.resolution")),
                "scale": str(conf.get("mglc.scale")),
                "mode": limits.mode,
                "aspect_ratio": aspect,
                "session_id": int(conf.get("mglc.session_id", 0)),
                "project_id": saved.project_id or 0,
                "episode": saved.episode or 0,
            },
            "fixed": {
                "video_model_id": model.code,
                "image_model_id": image_model_id,
                "duration_s": int(conf.get("mglc.duration_s") or 0),
                "num": int(conf.get("mglc.num", 1)),
            },
            "saved": jobs.generation_params_path(workdir, job).is_file(),
        }
        if refs is None:
            payload = {"output": [], "input": [], **common}
        else:
            ref_assets = refs_mod.load_refs(refs)
            videos = [a for a in ref_assets if a.kind == "video"]
            if videos and not limits.takes_video:
                raise UsageError(
                    f"model {model.code} mode {limits.mode} 不接受输入视频；"
                    "firstLastFrame 请只提交首尾帧图片")
            master = refs_mod.master(ref_assets)
            if master.kind == "video":
                master_spec = probe_input(master.path, conf)
            else:
                master_spec = probe_image(master.path, conf)
            if canvas is not None or source is not None:
                if canvas is None or source is None:
                    raise UsageError("--canvas 与 --source 要么都给，要么都不给")
                canvas_size = _parse_wh(canvas, "--canvas")
                _parse_wh(source, "--source")
            else:
                canvas_size = (master_spec.width, master_spec.height)
            input_options = (params_mod.input_options(
                canvas_size, master_spec, limits.ref_video_limits())
                if master.kind == "video" and limits.takes_video else [])
            if limits.mode == "firstLastFrame" or not videos:
                duration_s = 4
            elif limits.mode == "allMediaRef":
                specs = [probe_input(a.path, conf) for a in videos]
                duration_s = params_mod.derived_duration(
                    max(specs, key=lambda s: s.duration_s))
            else:
                duration_s = params_mod.derived_duration(master_spec)
            if aspect != "auto" and ":" in aspect:
                left, _, right = aspect.partition(":")
                ratio = int(left) / int(right)
            else:
                ratio = canvas_size[0] / canvas_size[1]
            outputs = params_mod.output_options(ratio)
            if limits.resolutions:
                outputs = [o for o in outputs if o["value"] in limits.resolutions]
            common["fixed"]["duration_s"] = duration_s
            payload = {
                "output": outputs,
                "input": input_options,
                **common,
            }
    except VrgenError as exc:
        _fail(exc)
    _echo_payload(payload)


@params_app.command("save")
def params_save(
    job: str = typer.Option(..., "--job", help="job 名（调用方通常传 case 名）。"),
    video_model_id: int = typer.Option(
        None, "--video-model-id",
        help="视频模型 ID（22 = Seedance 2.0 的 4K 路线）；不传保留已有值。"),
    image_model_id: int = typer.Option(
        None, "--image-model-id",
        help="图片模型 ID（首尾帧工作台与 360 静图用）；不传保留已有值。"),
    resolution: str = typer.Option(None, "--resolution",
                                   help="输出档位：480p | 720p | 1080p | 4K；省略保留已有值。"),
    scale: str = typer.Option(None, "--scale",
                              help="输入缩放：auto | full | half；省略保留已有值。"),
    mode: str = typer.Option(
        None, "--mode",
        help="生成模式（videoEdit/allMediaRef/videoExtend/firstLastFrame）；不传保留已有值。"),
    aspect_ratio: str = typer.Option(
        None, "--aspect-ratio", help="输出比例；auto = 跟随输入；不传保留已有值。"),
    session_id: int = typer.Option(
        None, "--session-id",
        help="项目生成会话 ID（>0）；0 = 个人额度；不传 = 保留已有设置。"),
    project_id: int = typer.Option(
        None, "--project-id", help="会话所属项目 ID（仅用于页面回显）。"),
    episode: int = typer.Option(
        None, "--episode", help="会话所属剧集序号（仅用于页面回显）。"),
    workdir: Path = typer.Option(None, "--workdir", help="台账根（默认 $VRGEN_RUNS_DIR）。"),
) -> None:
    """保存 job 级生成参数草稿（写 runs/<job>/generation.toml）。

    所有字段都可省略；省略时保留已有值。新建 gen case 只传 ``--mode``，分辨率与
    缩放在素材步骤完成后再写。
    """
    try:
        if resolution is not None and resolution not in params_mod.OUTPUT_TIERS:
            raise UsageError("resolution 只能是 480p | 720p | 1080p | 4K")
        if video_model_id is not None and video_model_id < 1:
            raise UsageError("video-model-id 必须是正整数")
        if image_model_id is not None and image_model_id < 1:
            raise UsageError("image-model-id 必须是正整数")
        if scale is not None and scale not in params_mod.INPUT_MODES:
            raise UsageError("scale 只能是 auto | full | half")
        if session_id is not None and session_id < 0:
            raise UsageError("session-id 必须 >= 0")
        if project_id is not None and project_id < 0:
            raise UsageError("project-id 必须 >= 0")
        if episode is not None and episode < 0:
            raise UsageError("episode 必须 >= 0")
        if all(v is None for v in (video_model_id, image_model_id, resolution,
                                   scale, mode, aspect_ratio,
                                   session_id, project_id, episode)):
            raise UsageError("没有要保存的参数")
        saved = jobs.load_generation_params(workdir, job)
        if mode is None:
            mode = saved.mode
        if aspect_ratio is None:
            aspect_ratio = saved.aspect_ratio
        if mode is not None and not str(mode).strip():
            raise UsageError("mode 不能为空")
        if aspect_ratio is not None:
            value = str(aspect_ratio).strip()
            if value != "auto" and ":" not in value:
                raise UsageError("aspect-ratio 必须是 W:H（如 16:9），或 auto")
            aspect_ratio = value
        if session_id is None:
            if project_id is not None or episode is not None:
                raise UsageError("改项目/剧集时请同时给 --session-id")
            session_id = saved.session_id
            project_id = saved.project_id
            episode = saved.episode
        elif session_id == 0:
            project_id = episode = 0
        else:
            if project_id is None:
                project_id = saved.project_id
            if episode is None:
                episode = saved.episode
            if episode and not project_id:
                raise UsageError("给了 --episode 就必须给 --project-id")
        effective_video_model = (video_model_id if video_model_id is not None
                                 else saved.video_model_id)
        effective_image_model = (image_model_id if image_model_id is not None
                                 else saved.image_model_id)
        effective_resolution = resolution if resolution is not None else saved.resolution
        effective_scale = scale if scale is not None else saved.scale
        jobs.save_generation_params(
            workdir, job,
            jobs.GenerationParams(video_model_id=effective_video_model,
                                  image_model_id=effective_image_model,
                                  resolution=effective_resolution, scale=effective_scale,
                                  mode=mode, aspect_ratio=aspect_ratio,
                                  session_id=session_id or None,
                                  project_id=project_id or None,
                                  episode=episode or None))
    except VrgenError as exc:
        _fail(exc)
    _echo_payload({"video_model_id": effective_video_model or 0,
                   "image_model_id": effective_image_model or 0,
                   "resolution": effective_resolution,
                   "scale": effective_scale,
                   "mode": mode, "aspect_ratio": aspect_ratio,
                   "session_id": session_id or 0,
                   "project_id": project_id or 0,
                   "episode": episode or 0})


# ── manual 后端（M3）─────────────────────────────────────────────────────

@app.command()
def register(
    job: str = typer.Option(..., "--job", help="job 名（调用方通常传 case 名）。"),
    file: Path = typer.Option(..., "--file", exists=True, dir_okay=False,
                              help="要登记为结果的外部文件。"),
    workdir: Path = typer.Option(None, "--workdir",
                                 help="台账根（默认 $VRGEN_RUNS_DIR 或 ./vrgen_runs）。"),
    notes: str = typer.Option("", "--notes", help="备注（进台账）。"),
) -> None:
    """手动后端：把本地文件注册为一轮结果，写同构台账。"""
    try:
        run = manual.register(job, file, workdir=workdir, notes=notes)
    except VrgenError as exc:
        _fail(exc)
    typer.echo(f"round    {run.dir.name}   gate={ (run.gate or {}).get('verdict')}")
    typer.echo(f"ledger   {run.dir}")
    typer.echo(f"next     vrgen result --job {job} --round {run.key[:12]} --out <路径>")


@app.command()
def status(
    job: str = typer.Option(..., "--job", help="job 名。"),
    workdir: Path = typer.Option(None, "--workdir", help="台账根。"),
) -> None:
    """列出该 job 的全部轮次（目录名 / 后端 / 阶段 / 闸门 / 时间）。"""
    try:
        rounds = jobs.list_rounds(workdir, job)
    except VrgenError as exc:
        _fail(exc)
    if not rounds:
        typer.echo(f"job {job}: 还没有 Session（记录根 {jobs.job_dir(workdir, job)}）")
        return
    typer.echo(f"job {job}   ({len(rounds)} 个 Session，新→旧)")
    for r in reversed(rounds):
        gate = (r.gate or {}).get("verdict", "?")
        backend = str(r.params.get("backend", "?"))
        mark = "*" if gate != "error" else "x"
        info = kinds_mod.KINDS.get(r.kind)
        label = f"{r.dir.name} [img]" if info is not None and info.kind == "image" else r.dir.name
        # result=yes/no：这个 Session 有没有生成好的结果——调用方（web）
        # 靠它预先判断"能不能选用"，而不是等 result 命令失败才知道
        has_result = jobs._has_result(r)
        if info is not None and info.multi_result and r.results:
            result_text = f"x{len(r.results)}"
        else:
            result_text = "yes" if has_result else "no"
        typer.echo(f"  {mark} {label:<22} {backend:<7} {r.stage:<10} "
                   f"gate={gate:<8} result={result_text:<3} {r.updated_utc}")


@app.command()
def result(
    job: str = typer.Option(..., "--job", help="job 名。"),
    out: Path = typer.Option(..., "--out", help="结果拷贝到这里。"),
    round_key: str = typer.Option("latest", "--round",
                                  help="轮次 key 或前缀；缺省取最新可用轮（gate != error）。"),
    index: int = typer.Option(None, "--index",
                              help="图片轮次取第 N 张候选（1 起）；缺省第一张。"),
    workdir: Path = typer.Option(None, "--workdir", help="台账根。"),
) -> None:
    """导出一轮结果（vrstudio adopt / 关键帧挑选时调用）。"""
    try:
        run = (jobs.latest_usable_round(workdir, job) if round_key == "latest"
               else jobs.find_round(workdir, job, round_key))
        src = jobs.result_path(run, index)
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, out)
    except VrgenError as exc:
        _fail(exc)
    picked = f"  index={index}" if index is not None else ""
    typer.echo(f"{out}   round={run.dir.name}{picked}   "
               f"gate={(run.gate or {}).get('verdict')}")
