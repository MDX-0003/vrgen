"""vrgen CLI 骨架。

命令面按契约（E:\\VRAIGC\\docs\\case-model.md §4）声明；当前全部为 stub，
逐个在 M1 实现。stub 的退出码固定为 2（参数/未实现），与契约的
"2 = 参数或配置错"语义一致，调用方（vrstudio/web 队列）可以统一处理。
"""

import typer

from . import __version__

app = typer.Typer(
    help="视频生成服务：mglc(Seedance) 与手动注册双后端，统一轮次台账。",
    no_args_is_help=True,
)

_NOT_IMPLEMENTED = (
    "尚未实现（M1 落地）。命令契约见 E:\\VRAIGC\\docs\\case-model.md §4。"
)


def _stub(name: str) -> None:
    typer.secho(f"vrgen {name}: {_NOT_IMPLEMENTED}", fg=typer.colors.YELLOW, err=True)
    raise typer.Exit(2)


@app.callback()
def _root(
    version: bool = typer.Option(False, "--version", help="打印版本并退出。"),
) -> None:
    if version:
        typer.echo(f"vrgen {__version__}")
        raise typer.Exit()


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
def status() -> None:
    """轮次状态；--watch 阻塞到终态。"""
    _stub("status")


@app.command()
def register() -> None:
    """手动后端：把本地文件注册为一轮结果，写同构台账。"""
    _stub("register")


@app.command()
def result() -> None:
    """导出一轮的结果文件（adopt 时由上游调用）。"""
    _stub("result")


@app.command()
def peek() -> None:
    """零额度查平台侧任务状态。"""
    _stub("peek")
