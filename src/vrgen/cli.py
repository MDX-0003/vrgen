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
from . import sessions as sessions_mod
from .errors import TaskFailed, UsageError, VrgenError
from .paths import templates_dir
from .platform.client import Mglc
from .platform.media import probe_input
from .platform.models import find_model, load_models
from .prep import params as params_mod
from .progress import Log
from .round.ledger import PROGRESS_LOG, RunStore, append_call_log, shell_line
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


def _apply_generation_params(conf, workdir, job, *, model_id, mode, resolution,
                             duration, num, aspect_ratio, scale, session_id=None) -> None:
    """Resolve submit parameters: CLI > generation.toml > config.

    Every option except ``scale`` maps one-to-one onto a ``mglc video generate``
    flag (``--mode`` is mglc ``--type``). ``scale`` is vrgen's own upload
    decision and has no mglc counterpart; it changes the prompt geometry.
    ``session_id`` selects a project generation session, so the spend is billed
    to that project's balance instead of the personal one.
    """
    saved = jobs.load_generation_params(workdir, job)
    picks = {
        "model_id": model_id,
        "mode": mode,
        "resolution": resolution if resolution is not None else saved.resolution,
        "duration_s": duration,
        "num": num,
        "aspect_ratio": aspect_ratio,
        "scale": scale if scale is not None else saved.scale,
        "session_id": session_id if session_id is not None else saved.session_id,
    }
    for key, value in picks.items():
        if value is not None:
            conf.data["mglc"][key] = value

    if int(conf.get("mglc.model_id")) < 1:
        raise UsageError("--model-id 必须是平台模型 ID（fullredraw 用 37）")
    if not str(conf.get("mglc.mode")).strip():
        raise UsageError("--mode 不能为空（fullredraw 用 videoEdit）")
    # CLI/草稿只接受页面上的三档；config 里仍可保留 quarter/WxH 供内部调试。
    if resolution is not None and resolution not in params_mod.OUTPUT_TIERS:
        raise UsageError("--resolution 只能是 480p | 720p | 1080p")
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
    if aspect != "auto" and ":" not in aspect:
        raise UsageError("--aspect-ratio 必须是 W:H（如 21:9），或 auto 表示跟随输入")
    if scale is not None and scale not in params_mod.INPUT_MODES:
        raise UsageError("--scale 只能是 auto | full | half")
    if saved.scale is not None and saved.scale not in params_mod.INPUT_MODES:
        raise UsageError(f"generation.toml: scale {saved.scale!r} 不合法")
    if int(conf.get("mglc.session_id", 0)) < 0:
        raise UsageError("--session-id 必须 >= 0（0 = 个人额度）")


def _parse_kv(items: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items or []:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise UsageError(f"--set 需要 key=value，got {item!r}")
        out[key.strip()] = value
    return out


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
    input: Path = typer.Option(..., "--input", exists=True, dir_okay=False,
                               help="送生成的视频（pad 画布或视口 clip）。"),
    canvas: str = typer.Option(..., "--canvas",
                               help="画布尺寸 WxH（提示词几何占位符的来源，如 4668x2000）。"),
    source: str = typer.Option(..., "--source",
                               help="源尺寸 WxH（冗余带宽度 = canvas 宽 - source 宽）。"),
    mask: Path = typer.Option(None, "--mask", exists=True, dir_okay=False,
                              help="区域标注图（可选）。"),
    template: str = typer.Option(
        None, "--template", help="模板 id；首次指定后记入 prompt-vars 草稿。"),
    sets: list[str] = typer.Option(None, "--set",
                                    help="提示词变量 k=v（自适应于草稿 [vars]）。"),
    model_id: int = typer.Option(None, "--model-id",
                                 help="← mglc --model-id；fullredraw 固定 37。"),
    mode: str = typer.Option(None, "--mode",
                             help="← mglc --type；fullredraw 固定 videoEdit。"),
    resolution: str = typer.Option(None, "--resolution",
                                    help="← mglc --resolution；输出档位 480p|720p|1080p。"),
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
    dry_run: bool = typer.Option(False, "--dry-run",
                                  help="只做本地半场（缩档+提示词+台账），不发请求不花钱。"),
    workdir: Path = typer.Option(None, "--workdir",
                                 help="台账根（默认 $VRGEN_RUNS_DIR）。"),
    config: Path = typer.Option(None, "--config", help="TOML 配置文件。"),
) -> None:
    """提交一轮生成：本地半场 → 素材登记 → 提交 → 轮询 → 下载 → 通用闸门。"""
    log = Log()
    try:
        conf = config_mod.load(config)
        _apply_generation_params(
            conf, workdir, job, model_id=model_id, mode=mode,
            resolution=resolution, duration=duration, num=num,
            aspect_ratio=aspect_ratio, scale=scale, session_id=session_id,
        )
        canvas_size = _parse_wh(canvas, "--canvas")
        source_size = _parse_wh(source, "--source")

        # prompt-vars 草稿 + --set（自适应校验）+ 模板解析（首次指定记入草稿）
        pv = jobs.load_prompt_vars(workdir, job)
        overrides = _parse_kv(sets)
        merged = jobs.apply_set(pv, overrides) if overrides else pv
        template_id = template or pv.template or str(conf.get("mglc.prompt_template"))
        if template and template != pv.template:
            jobs.save_prompt_vars(workdir, job,
                                  jobs.PromptVars(template=template, vars=pv.vars))
        if not (Path(templates_dir()) / f"{template_id}.md").is_file():
            available = sorted(p.stem for p in Path(templates_dir()).glob("*.md"))
            raise UsageError(f"模板 {template_id!r} 不存在；可用：{available}")

        pkg = InputPackage(job=job, input=Path(input), mask=Path(mask) if mask else None,
                           canvas_size=canvas_size, source_size=source_size,
                           workdir=jobs.job_dir(workdir, job), template=template_id,
                           variables=dict(merged.vars))
        prepared = prepare_round(conf, pkg, log=log)
    except VrgenError as exc:
        _fail(exc)

    # 从这里起每行同时落轮次目录：分钟级等待只有终端回滚不是记录。
    log.attach(prepared.run.dir / PROGRESS_LOG)

    print(f"job         {job}")
    print(f"round       {prepared.run.dir}")
    print(f"upload      {prepared.run.upload.get('path')}")
    print(f"            {prepared.geometry.describe()}")
    print(f"mask        {(prepared.run.mask or {}).get('path') or '(none)'}")
    print(f"model       {prepared.model.code} {prepared.model.name}  "
          f"mode {prepared.limits.mode}  {prepared.run.params['resolution']}  "
          f"{prepared.run.params['duration_s']}s  x{prepared.run.params['num']}")
    print(f"prompt      {prepared.run.prompt_path}\n")
    print(prepared.run.prompt)

    from .platform.binpath import find_mglc
    command = shell_line([find_mglc(conf) or "mglc", "video", "generate", *build_generate_argv(
        prompt=prepared.run.prompt,
        model_id=int(prepared.run.params["model_id"]),
        mode=str(prepared.run.params["mode"]),
        duration_s=int(prepared.run.params["duration_s"]),
        resolution=str(prepared.run.params["resolution"]),
        aspect_ratio=str(prepared.run.params["aspect_ratio"]),
        num=int(prepared.run.params.get("num", 1)),
        upload=prepared.run.upload.get("path", ""),
        mask=(prepared.run.mask or {}).get("path"),
        video_asset_id=prepared.run.assets.get("video"),
        image_asset_id=prepared.run.assets.get("image"),
        keep_sound=bool(prepared.run.params.get("keep_sound", True)),
        session_id=int(prepared.run.params.get("session_id", 0) or 0),
        preview=True)])
    print(f"\ncommand     {command}")

    if dry_run:
        print("\n--dry-run：什么都没发，没花额度。")
        _rounds_line(job, workdir)
        print(f"calls       {append_call_log(prepared.run, [], mode='vrgen submit --dry-run')}")
        return

    _wait_banner(conf, prepared)
    if prepared.run.task_id is not None:
        _announce_task(prepared.run.task_id, resumed=True)
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


def _announce_task(task_id: int, *, resumed: bool = False) -> None:
    """task id 一到手就交出去——等待以分钟计，用户该能在另一个终端瞟一眼。"""
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


@prompt_app.command("show")
def prompt_show(
    job: str = typer.Option(..., "--job", help="job 名（调用方通常传 case 名）。"),
    template: str = typer.Option(None, "--template", help="覆盖草稿/配置的模板选择。"),
    workdir: Path = typer.Option(None, "--workdir", help="台账根（默认 $VRGEN_RUNS_DIR）。"),
) -> None:
    """读模板与变量草稿：正文、占位符、几何键、[vars] 当前值。"""
    try:
        payload = jobs.prompt_overview(workdir, job, template=template,
                                       config_default=_prompt_default())
    except VrgenError as exc:
        _fail(exc)
    _echo_payload(payload)


@prompt_app.command("save")
def prompt_save(
    job: str = typer.Option(..., "--job", help="job 名（调用方通常传 case 名）。"),
    sets: list[str] = typer.Option(None, "--set",
                                   help="新增或覆盖变量 k=v（允许新键，直接写入草稿）。"),
    unsets: list[str] = typer.Option(None, "--unset",
                                     help="删除变量；模板正文引用的键拒绝删除。"),
    template: str = typer.Option(None, "--template", help="切换模板 id（必须存在）。"),
    workdir: Path = typer.Option(None, "--workdir", help="台账根（默认 $VRGEN_RUNS_DIR）。"),
) -> None:
    """编辑变量草稿并写回 prompt-vars.toml（JSON 回显保存后的状态）。"""
    try:
        payload = jobs.edit_prompt_vars(workdir, job, sets=_parse_kv(sets),
                                        unsets=unsets, template=template,
                                        config_default=_prompt_default())
    except VrgenError as exc:
        _fail(exc)
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
    input: Path = typer.Option(..., "--input", exists=True, dir_okay=False,
                               help="送生成的视频（用于计算 auto 输入尺寸）。"),
    canvas: str = typer.Option(..., "--canvas", help="画布尺寸 WxH。"),
    source: str = typer.Option(..., "--source", help="源尺寸 WxH（冗余带宽度来源）。"),
    aspect_ratio: str = typer.Option(
        None, "--aspect-ratio",
        help="覆盖 fixed 里的输出比例；auto 表示跟随输入（gen 流用）。"),
    config: Path = typer.Option(None, "--config", help="TOML 配置文件。"),
    workdir: Path = typer.Option(None, "--workdir", help="台账根（默认 $VRGEN_RUNS_DIR）。"),
) -> None:
    """输出分辨率档位 + 输入缩放档位的实际宽高，以及当前草稿/固定参数。"""
    try:
        conf = config_mod.load(config)
        _apply_generation_params(
            conf, workdir, job, model_id=None, mode=None, resolution=None,
            duration=None, num=None, aspect_ratio=aspect_ratio, scale=None,
        )
        canvas_size = _parse_wh(canvas, "--canvas")
        _parse_wh(source, "--source")
        models = load_models(jobs.job_dir(workdir, job),
                             url=str(conf.get("mglc.model_list_url")),
                             max_age_h=float(conf.get("mglc.models_max_age_h", 24.0)),
                             log=Log(quiet=True))
        model = find_model(models, int(conf.get("mglc.model_id")))
        limits = model.mode(str(conf.get("mglc.mode")))
        if not limits.takes_video:
            raise UsageError(f"model {model.code} mode {limits.mode} 不接受输入视频")
        spec = probe_input(input, conf)
        duration_s = int(conf.get("mglc.duration_s")) or params_mod.derived_duration(spec)
        saved = jobs.load_generation_params(workdir, job)
        outputs = params_mod.output_options(canvas_size[0] / canvas_size[1])
        if limits.resolutions:
            outputs = [o for o in outputs if o["value"] in limits.resolutions]
        payload = {
            "job": job,
            "output": outputs,
            "input": params_mod.input_options(canvas_size, spec,
                                              limits.ref_video_limits()),
            "current": {
                "resolution": str(conf.get("mglc.resolution")),
                "scale": str(conf.get("mglc.scale")),
                "session_id": int(conf.get("mglc.session_id", 0)),
                "project_id": saved.project_id or 0,
                "episode": saved.episode or 0,
            },
            "fixed": {
                "model_id": model.code,
                "mode": limits.mode,
                "aspect_ratio": str(conf.get("mglc.aspect_ratio")),
                "duration_s": duration_s,
                "num": int(conf.get("mglc.num", 1)),
            },
            "saved": jobs.generation_params_path(workdir, job).is_file(),
        }
    except VrgenError as exc:
        _fail(exc)
    _echo_payload(payload)


@params_app.command("save")
def params_save(
    job: str = typer.Option(..., "--job", help="job 名（调用方通常传 case 名）。"),
    resolution: str = typer.Option(..., "--resolution",
                                   help="输出档位：480p | 720p | 1080p。"),
    scale: str = typer.Option(..., "--scale",
                              help="输入缩放：auto | full | half。"),
    session_id: int = typer.Option(
        None, "--session-id",
        help="项目生成会话 ID（>0）；0 = 个人额度；不传 = 保留已有设置。"),
    project_id: int = typer.Option(
        None, "--project-id", help="会话所属项目 ID（仅用于页面回显）。"),
    episode: int = typer.Option(
        None, "--episode", help="会话所属剧集序号（仅用于页面回显）。"),
    workdir: Path = typer.Option(None, "--workdir", help="台账根（默认 $VRGEN_RUNS_DIR）。"),
) -> None:
    """保存 job 级生成参数草稿（写 runs/<job>/generation.toml）。"""
    try:
        if resolution not in params_mod.OUTPUT_TIERS:
            raise UsageError("resolution 只能是 480p | 720p | 1080p")
        if scale not in params_mod.INPUT_MODES:
            raise UsageError("scale 只能是 auto | full | half")
        if session_id is not None and session_id < 0:
            raise UsageError("session-id 必须 >= 0")
        if project_id is not None and project_id < 0:
            raise UsageError("project-id 必须 >= 0")
        if episode is not None and episode < 0:
            raise UsageError("episode 必须 >= 0")
        saved = jobs.load_generation_params(workdir, job)
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
        jobs.save_generation_params(
            workdir, job,
            jobs.GenerationParams(resolution=resolution, scale=scale,
                                  session_id=session_id or None,
                                  project_id=project_id or None,
                                  episode=episode or None))
    except VrgenError as exc:
        _fail(exc)
    _echo_payload({"resolution": resolution, "scale": scale,
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
        # result=yes/no：这个 Session 有没有生成好的结果视频——调用方（web）
        # 靠它预先判断"能不能选用"，而不是等 result 命令失败才知道
        has_result = r.result_path is not None and r.result_path.is_file()
        typer.echo(f"  {mark} {r.dir.name:<22} {backend:<7} {r.stage:<10} "
                   f"gate={gate:<8} result={'yes' if has_result else 'no':<3} {r.updated_utc}")


@app.command()
def result(
    job: str = typer.Option(..., "--job", help="job 名。"),
    out: Path = typer.Option(..., "--out", help="结果拷贝到这里。"),
    round_key: str = typer.Option("latest", "--round",
                                  help="轮次 key 或前缀；缺省取最新可用轮（gate != error）。"),
    workdir: Path = typer.Option(None, "--workdir", help="台账根。"),
) -> None:
    """导出一轮结果（vrstudio adopt 时调用）。"""
    try:
        run = (jobs.latest_usable_round(workdir, job) if round_key == "latest"
               else jobs.find_round(workdir, job, round_key))
        src = jobs.result_path(run)
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, out)
    except VrgenError as exc:
        _fail(exc)
    typer.echo(f"{out}   round={run.dir.name}   gate={(run.gate or {}).get('verdict')}")
