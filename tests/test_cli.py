"""CLI 冒烟与契约测试（M1 后 submit 仍以离线方式测本地半场，见 test_mglc.py）。"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from vrgen import jobs
from vrgen.cli import app

runner = CliRunner()

REAL_COMMANDS = {"register", "status", "result", "submit", "login", "models",
                 "peek", "prompt"}


def test_help_lists_the_contract_surface():
    r = runner.invoke(app, ["--help"])
    assert r.exit_code == 0
    for cmd in REAL_COMMANDS:
        assert cmd in r.output, f"契约命令缺失: {cmd}"


def test_version():
    r = runner.invoke(app, ["--version"])
    assert r.exit_code == 0
    assert "vrgen" in r.output


def test_register_status_result_chain(tmp_path, util_video):
    out = tmp_path / "adopted.mp4"
    r = runner.invoke(app, ["register", "--job", "cli_job", "--file", str(util_video),
                            "--workdir", str(tmp_path / "runs")])
    assert r.exit_code == 0, r.output
    assert "round" in r.output and "vrgen result" in r.output

    r = runner.invoke(app, ["status", "--job", "cli_job",
                            "--workdir", str(tmp_path / "runs")])
    assert r.exit_code == 0 and "manual" in r.output

    r = runner.invoke(app, ["result", "--job", "cli_job", "--out", str(out),
                            "--workdir", str(tmp_path / "runs")])
    assert r.exit_code == 0, r.output
    assert out.is_file() and out.read_bytes() == Path(util_video).read_bytes()


def test_result_missing_round_is_runtime_error(tmp_path):
    r = runner.invoke(app, ["result", "--job", "nope", "--out",
                            str(tmp_path / "o.mp4"), "--workdir", str(tmp_path)])
    assert r.exit_code == 1          # 没有可用轮 = 运行失败（不是参数错）


def test_bad_job_name_is_usage_error(tmp_path):
    r = runner.invoke(app, ["register", "--job", "../evil", "--file",
                            str(tmp_path / "f.bin"), "--workdir", str(tmp_path)])
    assert r.exit_code == 2


def test_submit_rejects_bad_geometry(tmp_path, util_video):
    r = runner.invoke(app, ["submit", "--job", "j", "--input", str(util_video),
                            "--canvas", "46x68", "--source", "4000x2000",
                            "--workdir", str(tmp_path)])
    assert r.exit_code == 1          # band 装不进 canvas → 运行失败
    r = runner.invoke(app, ["submit", "--job", "j", "--input", str(util_video),
                            "--canvas", "garbage", "--source", "4000x2000",
                            "--workdir", str(tmp_path)])
    assert r.exit_code == 2          # 参数格式错


def test_submit_unknown_variable_is_usage_error(tmp_path, util_video):
    r = runner.invoke(app, ["submit", "--job", "j", "--input", str(util_video),
                            "--canvas", "4668x2000", "--source", "4000x2000",
                            "--set", "nope=1", "--workdir", str(tmp_path)])
    assert r.exit_code == 2
    assert "未知变量" in r.output or "先在" in r.output


# ── prompt show / save（决策 18，契约 case-model.md §4）───────────────────


def test_prompt_show_defaults_and_placeholders(tmp_path):
    r = runner.invoke(app, ["prompt", "show", "--job", "j1",
                            "--workdir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["template"] == "360-closure"        # 配置默认兜底
    assert data["vars"] == {}                       # 还没有草稿
    assert "theme" in data["placeholders"] and "style" in data["placeholders"]
    assert "{canvas_w}" in data["template_text"]
    assert "canvas_w" in data["geometry_keys"] and "canvas_w" not in data["vars"]
    assert "360-closure" in data["available"]


def test_prompt_save_roundtrip_with_new_keys(tmp_path):
    wd = str(tmp_path)
    r = runner.invoke(app, ["prompt", "save", "--job", "j1", "--set", "theme=黄昏",
                            "--set", "mood=安静", "--workdir", wd])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["vars"] == {"theme": "黄昏", "mood": "安静"}   # 新键直接入草稿
    pv = jobs.load_prompt_vars(tmp_path, "j1")                 # 落盘可读回
    assert pv.template == "360-closure" and pv.vars["mood"] == "安静"

    r = runner.invoke(app, ["prompt", "show", "--job", "j1", "--workdir", wd])
    assert json.loads(r.output)["vars"] == {"theme": "黄昏", "mood": "安静"}


def test_prompt_save_protects_template_keys_and_rejects_unknown_unset(tmp_path):
    wd = str(tmp_path)
    runner.invoke(app, ["prompt", "save", "--job", "j1", "--set", "theme=t",
                        "--set", "extra=e", "--workdir", wd])
    r = runner.invoke(app, ["prompt", "save", "--job", "j1", "--unset", "theme",
                            "--workdir", wd])
    assert r.exit_code == 2 and "不能删除" in r.output
    r = runner.invoke(app, ["prompt", "save", "--job", "j1", "--unset", "nope",
                            "--workdir", wd])
    assert r.exit_code == 2 and "未知变量" in r.output
    r = runner.invoke(app, ["prompt", "save", "--job", "j1", "--unset", "extra",
                            "--workdir", wd])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["vars"] == {"theme": "t"}


def test_prompt_save_rejects_unknown_template(tmp_path):
    r = runner.invoke(app, ["prompt", "save", "--job", "j1", "--template", "no-such",
                            "--workdir", str(tmp_path)])
    assert r.exit_code == 2 and "不存在" in r.output
    assert not jobs.prompt_vars_path(tmp_path, "j1").is_file()   # 失败不落盘
