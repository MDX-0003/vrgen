"""冒烟：命令面必须与 docs/case-model.md §4 的契约一致。

这些断言是契约的守卫——增删子命令而不同步改文档（或反过来）会在这里红。
"""

from typer.testing import CliRunner

from vrgen.cli import app

runner = CliRunner()

EXPECTED_COMMANDS = {
    "login", "models", "submit", "status", "register", "result", "peek",
}


def test_help_lists_the_contract_surface():
    r = runner.invoke(app, ["--help"])
    assert r.exit_code == 0
    for cmd in EXPECTED_COMMANDS:
        assert cmd in r.output, f"契约命令缺失: {cmd}"


def test_version():
    r = runner.invoke(app, ["--version"])
    assert r.exit_code == 0
    assert "vrgen" in r.output


def test_stubs_exit_2_until_implemented():
    for cmd in sorted(EXPECTED_COMMANDS):
        r = runner.invoke(app, [cmd])
        assert r.exit_code == 2, f"`vrgen {cmd}` 应以退出码 2 表示未实现，got {r.exit_code}"
        assert "尚未实现" in r.output
