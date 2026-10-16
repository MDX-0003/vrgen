"""vrgen CLI。

命令契约（E:\\VRAIGC\\docs\\case-model.md §4）：
- M3 已实现：register / status / result（manual 后端全链路）
- M1 待实现：login / models / submit / peek（mglc 平台链路，stub 退出码 2）

退出码：0 成功 / 2 参数或配置错（UsageError）/ 1 运行失败（VrgenError，台账有现场）。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import typer

from . import __version__, jobs, manual
from .errors import UsageError, VrgenError

app = typer.Typer(
    help="视频生成服务：mglc(Seedance) 与手动注册双后端，统一轮次台账。",
    no_args_is_help=True,
    invoke_without_command=True,
)

_NOT_IMPLEMENTED = "尚未实现（M1 落地）。命令契约见 E:\\VRAIGC\\docs\\case-model.md §4。"


def _stub(name: str) -> None:
    typer.secho(f"vrgen {name}: {_NOT_IMPLEMENTED}", fg=typer.colors.YELLOW, err=True)
    raise typer.Exit(2)


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


# ── M3：manual 后端全链路 ────────────────────────────────────────────────


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
        record = manual.register(job, file, workdir=workdir, notes=notes)
    except VrgenError as exc:
        _fail(exc)
    typer.echo(f"round    {record.round}   gate={record.gate.get('verdict')}")
    typer.echo(f"ledger   {jobs.round_dir(workdir, job, record.round)}")
    typer.echo(f"next     vrgen result --job {job} --round {record.round} --out <路径>")


@app.command()
def status(
    job: str = typer.Option(..., "--job", help="job 名。"),
    workdir: Path = typer.Option(None, "--workdir", help="台账根。"),
) -> None:
    """列出该 job 的全部轮次（key / 后端 / 阶段 / 闸门 / 时间）。"""
    try:
        rounds = jobs.list_rounds(workdir, job)
    except VrgenError as exc:
        _fail(exc)
    if not rounds:
        typer.echo(f"job {job}: 还没有轮次（台账根 {jobs.resolve_workdir(workdir)}）")
        return
    typer.echo(f"job {job}   ({len(rounds)} 轮，新→旧)")
    for r in reversed(rounds):
        gate = r.gate.get("verdict", "?")
        size = r.result.get("bytes")
        size_s = f"{size / 1e6:.1f}MB" if isinstance(size, (int, float)) else "-"
        mark = "*" if gate != "error" else "x"
        typer.echo(f"  {mark} {r.round:<22} {r.backend:<7} {r.stage:<8} "
                   f"gate={gate:<8} {size_s:>8}  {r.created_utc}")


@app.command()
def result(
    job: str = typer.Option(..., "--job", help="job 名。"),
    out: Path = typer.Option(..., "--out", help="结果拷贝到这里。"),
    round_key: str = typer.Option("latest", "--round",
                                  help="轮次 key；缺省取最新可用轮（gate != error）。"),
    workdir: Path = typer.Option(None, "--workdir", help="台账根。"),
) -> None:
    """导出一轮结果（vrstudio adopt 时调用）。"""
    try:
        record = (jobs.latest_usable_round(workdir, job) if round_key == "latest"
                  else jobs.find_round(workdir, job, round_key))
        src = jobs.result_path(record, workdir)
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, out)
    except VrgenError as exc:
        _fail(exc)
    typer.echo(f"{out}   round={record.round}   gate={record.gate.get('verdict')}")


# ── M1：mglc 平台链路（stub）────────────────────────────────────────────


@app.command()
def login() -> None:
    """登录态管理（透传 mglc）。"""
    _stub("login")


@app.command()
def models() -> None:
    """平台模型清单（24h 缓存）。"""
    _stub("models")


@app.command()
def submit() -> None:
    """提交一轮生成：输入包(视频+mask+提示词+分辨率档)，断点续跑、task_id 防重发。"""
    _stub("submit")


@app.command()
def peek() -> None:
    """零额度查平台侧任务状态。"""
    _stub("peek")
