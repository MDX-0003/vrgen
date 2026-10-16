"""CLI 冒烟与契约测试。

- 命令面必须与 docs/case-model.md §4 一致（真实命令 --help 可用）；
- M1 的 stub（login/models/submit/peek）退出码 2；
- register → status → result 的 manual 链路端到端走通 CLI 层。
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from vrgen.cli import app

runner = CliRunner()

REAL_COMMANDS = {"register", "status", "result"}
STUB_COMMANDS = {"login", "models", "submit", "peek"}


def test_help_lists_the_contract_surface():
    r = runner.invoke(app, ["--help"])
    assert r.exit_code == 0
    for cmd in REAL_COMMANDS | STUB_COMMANDS:
        assert cmd in r.output, f"契约命令缺失: {cmd}"


def test_version():
    r = runner.invoke(app, ["--version"])
    assert r.exit_code == 0
    assert "vrgen" in r.output


def test_stubs_exit_2_until_implemented():
    for cmd in sorted(STUB_COMMANDS):
        r = runner.invoke(app, [cmd])
        assert r.exit_code == 2, f"`vrgen {cmd}` 应以退出码 2 表示未实现，got {r.exit_code}"
        assert "尚未实现" in r.output


def test_register_status_result_chain(tmp_path, util_video):
    out = tmp_path / "adopted.mp4"
    r = runner.invoke(app, ["register", "--job", "cli_job", "--file", str(util_video),
                            "--workdir", str(tmp_path / "runs")])
    assert r.exit_code == 0, r.output
    assert "round" in r.output and "vrgen result" in r.output

    r = runner.invoke(app, ["status", "--job", "cli_job",
                            "--workdir", str(tmp_path / "runs")])
    assert r.exit_code == 0 and "manual-" in r.output

    r = runner.invoke(app, ["result", "--job", "cli_job", "--out", str(out),
                            "--workdir", str(tmp_path / "runs")])
    assert r.exit_code == 0, r.output
    assert out.is_file() and out.read_bytes() == Path(util_video).read_bytes()


def test_result_missing_round_is_usage_error(tmp_path):
    r = runner.invoke(app, ["result", "--job", "nope", "--out",
                            str(tmp_path / "o.mp4"), "--workdir", str(tmp_path)])
    assert r.exit_code == 1          # 没有可用轮 = 运行失败（不是参数错）


def test_bad_job_name_is_usage_error(tmp_path):
    r = runner.invoke(app, ["register", "--job", "../evil", "--file",
                            str(tmp_path / "f.bin"), "--workdir", str(tmp_path)])
    assert r.exit_code == 2
