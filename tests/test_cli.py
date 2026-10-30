"""CLI 冒烟与契约测试（M1 后 submit 仍以离线方式测本地半场，见 test_mglc.py）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from vrgen import cli as vrgen_cli
from vrgen import jobs
from vrgen.cli import app
from vrgen.errors import UsageError
from vrgen.prep.budget import InputSpec

runner = CliRunner()

REAL_COMMANDS = {"register", "status", "result", "submit", "login", "models",
                 "peek", "prompt", "params", "sessions", "image"}


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


def _refs_file(tmp_path, video):
    path = tmp_path / "refs.json"
    path.write_text(json.dumps({"assets": [
        {"id": "clip", "kind": "video", "role": "edit_master", "path": str(video)},
    ]}, ensure_ascii=False), encoding="utf-8")
    return path


def test_submit_rejects_bad_arguments(tmp_path, util_video):
    refs = _refs_file(tmp_path, util_video)
    r = runner.invoke(app, ["submit", "--job", "j", "--refs", str(refs),
                            "--canvas", "garbage", "--source", "4000x2000",
                            "--workdir", str(tmp_path)])
    assert r.exit_code == 2          # 参数格式错
    r = runner.invoke(app, ["submit", "--job", "j",
                            "--canvas", "4668x2000", "--source", "4000x2000",
                            "--workdir", str(tmp_path)])
    assert r.exit_code == 2          # 缺 --refs（唯一提交入口）
    r = runner.invoke(app, ["submit", "--job", "j", "--refs", str(refs),
                            "--canvas", "4668x2000", "--workdir", str(tmp_path)])
    assert r.exit_code == 2          # canvas/source 必须成对


def test_submit_unknown_variable_is_usage_error(tmp_path, util_video):
    r = runner.invoke(app, ["submit", "--job", "j", "--refs",
                            str(_refs_file(tmp_path, util_video)),
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
    for key in ("plot", "action", "allowed_changes"):
        assert key in data["placeholders"]
    assert "【情节与动作】" in data["template_text"]
    assert "【允许变化】" in data["template_text"]
    assert "{canvas_w}" in data["template_text"]
    assert "canvas_w" in data["geometry_keys"] and "canvas_w" not in data["vars"]
    assert data["labels"]["theme"] == "主题 / 场景"     # 模板别名（契约 §4）
    assert data["labels"]["plot"] == "情节"
    assert data["help"]["theme"].startswith("写场景主题")
    assert "360-closure" in data["available"]


def test_prompt_show_renders_with_refs_and_geometry(tmp_path):
    jobs.save_prompt_vars(tmp_path, "j1", jobs.PromptVars(template="gen-multi", vars={
        "subject": "高官从起点走到终点。",
        "target": "老年高官。",
        "plot": "镜头1：坐。镜头2：起身。",
        "preserve": "镜头固定。",
        "allowed_changes": "只换人。",
    }))
    video = tmp_path / "v.mp4"
    video.write_bytes(b"v")
    image = tmp_path / "i.png"
    image.write_bytes(b"i")
    refs = tmp_path / "refs.json"
    refs.write_text(json.dumps({"assets": [
        {"id": "v1", "kind": "video", "role": "space_ref", "path": str(video)},
        {"id": "i1", "kind": "image", "role": "subject_ref", "path": str(image)},
    ]}, ensure_ascii=False), encoding="utf-8")

    r = runner.invoke(app, ["prompt", "show", "--job", "j1", "--template", "gen-multi",
                            "--workdir", str(tmp_path), "--refs", str(refs),
                            "--mode", "allMediaRef",
                            "--canvas", "1920x1052", "--source", "1920x1052"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["render_error"] is None
    rendered = data["rendered"]
    assert "@Video1：空间参考" in rendered
    assert "锁定参考素材的空间" in rendered
    assert "1920×1052" in rendered
    assert "高官从起点走到终点" in rendered
    assert "{" not in rendered


def test_prompt_show_reports_missing_variable(tmp_path):
    jobs.save_prompt_vars(tmp_path, "j1", jobs.PromptVars(template="gen-multi", vars={
        "subject": "主体",
    }))
    r = runner.invoke(app, ["prompt", "show", "--job", "j1", "--template", "gen-multi",
                            "--workdir", str(tmp_path),
                            "--canvas", "1920x1052", "--source", "1920x1052"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["rendered"] is None
    assert "asset_list" in data["render_error"]


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


# ── params show / save（决策 20，契约 case-model.md §4）───────────────────


class _Limits:
    mode = "videoEdit"
    takes_video = True
    resolutions = ("480p", "720p", "1080p")
    ratios = ()
    max_count = 1
    duration = {"type": "range", "min": 4, "max": 30}

    def ref_video_limits(self):
        return {"minPixel": 409_600, "maxPixel": 8_295_044,
                "maxBytes": 209_715_200,
                "fps": {"min": 24, "max": 60},
                "duration": {"min": 2, "max": 30},
                "formats": ["mp4"],
                "refVideoMinAspectRatio": 0.4,
                "refVideoMaxAspectRatio": 2.5}


class _Model:
    code = 37
    modes = {"videoEdit": _Limits()}

    def mode(self, name):
        return _Limits()


def _params_offline(tmp_path, monkeypatch):
    video = tmp_path / "in.mp4"
    video.write_bytes(b"fake")
    refs = tmp_path / "refs.json"
    refs.write_text(json.dumps({"assets": [
        {"id": "clip", "kind": "video", "role": "edit_master", "path": str(video)},
    ]}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(vrgen_cli, "load_models", lambda *a, **kw: [_Model()])
    monkeypatch.setattr(vrgen_cli, "find_model", lambda models, code: _Model())
    monkeypatch.setattr(vrgen_cli, "probe_input", lambda path, conf: InputSpec(
        width=4668, height=2000, fps=30.0, duration_s=7.979,
        size_bytes=228_200_000, container=("mp4",)))
    monkeypatch.setattr(vrgen_cli, "probe_image", lambda path, conf: InputSpec(
        width=1920, height=1080, fps=0.0, duration_s=0.0,
        size_bytes=500_000, container=("png",)))
    return refs


def test_params_show_without_refs_returns_modes_only(tmp_path, monkeypatch):
    _params_offline(tmp_path, monkeypatch)
    r = runner.invoke(app, ["params", "show", "--job", "j1",
                            "--workdir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["output"] == [] and data["input"] == []
    assert data["modes"][0]["value"] == "videoEdit"
    # 角色选项来自全局词表，不再按模式回显矩阵。
    assert "roles" not in data["modes"][0]


class _FirstLastLimits:
    mode = "firstLastFrame"
    takes_video = False
    resolutions = ("480p", "720p", "1080p")
    ratios = ()
    max_count = 2
    duration = {"type": "range", "min": 4, "max": 30}

    def ref_image_limits(self):
        return {"minPixel": 90000, "maxPixel": 36000000,
                "maxBytes": 31457280, "maxCount": 2}


class _FirstLastModel:
    code = 37
    modes = {"videoEdit": _Limits(), "firstLastFrame": _FirstLastLimits()}

    def mode(self, name):
        return self.modes[name]


def test_params_show_first_last_frame_without_input(tmp_path, monkeypatch):
    _params_offline(tmp_path, monkeypatch)
    monkeypatch.setattr(vrgen_cli, "load_models",
                        lambda *a, **kw: [_FirstLastModel()])
    monkeypatch.setattr(vrgen_cli, "find_model",
                        lambda models, code: _FirstLastModel())
    jobs.save_generation_params(
        tmp_path, "j1", jobs.GenerationParams(mode="firstLastFrame"))

    r = runner.invoke(app, ["params", "show", "--job", "j1",
                            "--workdir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["current"]["mode"] == "firstLastFrame"
    modes = {m["value"]: m for m in data["modes"]}
    assert modes["firstLastFrame"]["takes_video"] is False
    assert modes["firstLastFrame"]["video_required"] is False
    assert modes["firstLastFrame"]["max_videos"] == 0
    assert modes["firstLastFrame"]["duration"] == {"type": "range", "min": 4, "max": 30}


def test_params_show_first_last_frame_rejects_input_video(tmp_path, monkeypatch):
    refs = _params_offline(tmp_path, monkeypatch)
    monkeypatch.setattr(vrgen_cli, "load_models",
                        lambda *a, **kw: [_FirstLastModel()])
    monkeypatch.setattr(vrgen_cli, "find_model",
                        lambda models, code: _FirstLastModel())
    jobs.save_generation_params(
        tmp_path, "j1", jobs.GenerationParams(mode="firstLastFrame"))

    r = runner.invoke(app, ["params", "show", "--job", "j1",
                            "--refs", str(refs), "--canvas", "1920x1080",
                            "--source", "1920x1080", "--workdir", str(tmp_path)])
    assert r.exit_code == 2, r.output
    assert "不接受输入视频" in r.output


def test_params_save_accepts_mode_only(tmp_path):
    wd = str(tmp_path)
    r = runner.invoke(app, ["params", "save", "--job", "j1",
                            "--mode", "allMediaRef", "--workdir", wd])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["mode"] == "allMediaRef"
    text = jobs.generation_params_path(tmp_path, "j1").read_text(encoding="utf-8")
    assert 'mode = "allMediaRef"' in text


def test_prompt_show_marks_gen_multi_computed_keys(tmp_path):
    from vrgen import jobs as jobs_mod
    payload = jobs_mod.prompt_overview(tmp_path, "j1", template="gen-multi")
    assert set(payload["computed_keys"]) == {"asset_list", "task_rule"}
    with pytest.raises(UsageError, match="自动生成"):
        jobs_mod.edit_prompt_vars(tmp_path, "j1", sets={"asset_list": "x"},
                                  template="gen-multi")


def test_params_show_reports_both_resolution_kinds(tmp_path, monkeypatch):
    refs = _params_offline(tmp_path, monkeypatch)
    r = runner.invoke(app, ["params", "show", "--job", "j1",
                            "--refs", str(refs), "--canvas", "4668x2000",
                            "--source", "4000x2000", "--workdir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["output"][1] == {"value": "720p", "width": 1470, "height": 630}
    sizes = {o["value"]: (o["width"], o["height"], o["ok"]) for o in data["input"]}
    assert sizes["auto"] == (4400, 1884, True)
    assert sizes["full"] == (4668, 2000, False)       # 像素和体积都超限
    assert sizes["half"] == (2334, 1000, True)
    assert data["current"] == {"resolution": "1080p", "scale": "auto",
                               "mode": "videoEdit", "aspect_ratio": "21:9",
                               "session_id": 0, "project_id": 0, "episode": 0}
    assert data["fixed"] == {"model_id": 37, "duration_s": 8, "num": 1}
    assert data["modes"][0]["value"] == "videoEdit"
    assert data["modes"][0]["ratios"] == []
    assert data["fixed"]["duration_s"] == 8
    assert data["saved"] is False


class _AllMedia4KLimits:
    mode = "allMediaRef"
    takes_video = True
    resolutions = ("480p", "720p", "1080p", "4K")
    ratios = ("auto", "1:1", "16:9", "21:9")
    max_count = 15
    duration = {"type": "range", "min": 4, "max": 15}

    def ref_video_limits(self):
        return {"minPixel": 409_600, "maxPixel": 2_086_668,
                "maxBytes": 209_715_200,
                "fps": {"min": 24, "max": 60},
                "duration": {"min": 2, "max": 15},
                "formats": ["mp4"],
                "refVideoMinAspectRatio": 0.4,
                "refVideoMaxAspectRatio": 2.5,
                "maxCount": 3}

    def ref_image_limits(self):
        return {"maxBytes": 0, "maxCount": 9}


class _AllMedia4KModel:
    code = 22
    name = "Seedance 2.0"
    modes = {"allMediaRef": _AllMedia4KLimits()}

    def mode(self, name):
        return self.modes[name]


def _four_k_show(tmp_path, monkeypatch, aspect: str) -> dict:
    refs = _params_offline(tmp_path, monkeypatch)
    monkeypatch.setattr(vrgen_cli, "load_models",
                        lambda *a, **kw: [_Model(), _AllMedia4KModel()])
    monkeypatch.setattr(
        vrgen_cli, "find_model",
        lambda models, code: _AllMedia4KModel() if int(code) == 22 else _Model())
    jobs.save_generation_params(
        tmp_path, "j1", jobs.GenerationParams(model_id=22, mode="allMediaRef"))
    r = runner.invoke(app, ["params", "show", "--job", "j1",
                            "--refs", str(refs),
                            "--canvas", "1920x1080",
                            "--source", "1920x1080",
                            "--aspect-ratio", aspect,
                            "--workdir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    return json.loads(r.output)


def test_params_show_offers_4k_when_the_model_publishes_it(tmp_path, monkeypatch):
    data = _four_k_show(tmp_path, monkeypatch, "16:9")
    assert {"value": "4K", "width": 3840, "height": 2160} in data["output"]
    assert data["fixed"]["model_id"] == 22


def test_params_show_skips_4k_for_21_9_without_a_nominal(tmp_path, monkeypatch):
    data = _four_k_show(tmp_path, monkeypatch, "21:9")
    assert all(item["value"] != "4K" for item in data["output"])


def test_output_options_4k_is_16_9_only():
    from vrgen.prep import params as params_mod
    assert params_mod.output_options(16 / 9)[-1] == {
        "value": "4K", "width": 3840, "height": 2160}
    assert all(item["value"] != "4K"
               for item in params_mod.output_options(21 / 9))


def test_params_save_model_id_switches_the_capability_view(tmp_path, monkeypatch):
    refs = _params_offline(tmp_path, monkeypatch)
    monkeypatch.setattr(vrgen_cli, "load_models",
                        lambda *a, **kw: [_Model(), _AllMedia4KModel()])
    monkeypatch.setattr(
        vrgen_cli, "find_model",
        lambda models, code: _AllMedia4KModel() if int(code) == 22 else _Model())
    r = runner.invoke(app, ["params", "save", "--job", "j1",
                            "--model-id", "22", "--mode", "allMediaRef",
                            "--resolution", "4K", "--aspect-ratio", "16:9",
                            "--workdir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["model_id"] == 22
    assert 'model_id = 22' in jobs.generation_params_path(
        tmp_path, "j1").read_text(encoding="utf-8")

    r = runner.invoke(app, ["params", "show", "--job", "j1", "--refs", str(refs),
                            "--canvas", "1920x1080", "--source", "1920x1080",
                            "--workdir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["fixed"]["model_id"] == 22
    assert data["current"]["mode"] == "allMediaRef"
    assert data["current"]["aspect_ratio"] == "16:9"
    assert {"value": "4K", "width": 3840, "height": 2160} in data["output"]


def test_params_save_roundtrips_through_show(tmp_path, monkeypatch):
    refs = _params_offline(tmp_path, monkeypatch)
    wd = str(tmp_path)
    r = runner.invoke(app, ["params", "save", "--job", "j1",
                            "--resolution", "720p", "--scale", "half",
                            "--workdir", wd])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output) == {"model_id": 0,
                                    "resolution": "720p", "scale": "half",
                                    "mode": None, "aspect_ratio": None,
                                    "session_id": 0, "project_id": 0, "episode": 0}
    assert jobs.generation_params_path(tmp_path, "j1").is_file()

    r = runner.invoke(app, ["params", "show", "--job", "j1",
                            "--refs", str(refs), "--canvas", "4668x2000",
                            "--source", "4000x2000", "--workdir", wd])
    data = json.loads(r.output)
    assert data["current"]["resolution"] == "720p"
    assert data["current"]["scale"] == "half"
    assert data["current"]["mode"] == "videoEdit"
    assert data["saved"] is True


def test_params_save_persists_project_and_episode_for_the_ui(tmp_path, monkeypatch):
    refs = _params_offline(tmp_path, monkeypatch)
    wd = str(tmp_path)
    r = runner.invoke(app, ["params", "save", "--job", "j1",
                            "--resolution", "1080p", "--scale", "auto",
                            "--session-id", "2060150528006080",
                            "--project-id", "2060150527854528",
                            "--episode", "1", "--workdir", wd])
    assert r.exit_code == 0, r.output
    echo = json.loads(r.output)
    assert echo["project_id"] == 2060150527854528 and echo["episode"] == 1

    r = runner.invoke(app, ["params", "show", "--job", "j1",
                            "--refs", str(refs), "--canvas", "1920x1052",
                            "--source", "1920x1052", "--workdir", wd])
    current = json.loads(r.output)["current"]
    assert current["session_id"] == 2060150528006080
    assert current["project_id"] == 2060150527854528
    assert current["episode"] == 1

    # 切回个人额度：三项一起清空
    r = runner.invoke(app, ["params", "save", "--job", "j1",
                            "--resolution", "1080p", "--scale", "auto",
                            "--session-id", "0", "--workdir", wd])
    echo = json.loads(r.output)
    assert (echo["session_id"], echo["project_id"], echo["episode"]) == (0, 0, 0)


def test_params_save_persists_project_session_id(tmp_path, monkeypatch):
    refs = _params_offline(tmp_path, monkeypatch)
    wd = str(tmp_path)
    r = runner.invoke(app, ["params", "save", "--job", "j1",
                            "--resolution", "1080p", "--scale", "auto",
                            "--session-id", "2060150528006080", "--workdir", wd])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["session_id"] == 2060150528006080

    r = runner.invoke(app, ["params", "show", "--job", "j1",
                            "--refs", str(refs), "--canvas", "1920x1052",
                            "--source", "1920x1052", "--workdir", wd])
    data = json.loads(r.output)
    assert data["current"]["session_id"] == 2060150528006080
    # 不传 session-id 再保存，保留已有会话
    r = runner.invoke(app, ["params", "save", "--job", "j1",
                            "--resolution", "720p", "--scale", "half",
                            "--workdir", wd])
    assert json.loads(r.output)["session_id"] == 2060150528006080


def test_params_show_aspect_auto_is_reported_for_gen(tmp_path, monkeypatch):
    refs = _params_offline(tmp_path, monkeypatch)
    r = runner.invoke(app, ["params", "show", "--job", "j1",
                            "--refs", str(refs), "--canvas", "1920x1052",
                            "--source", "1920x1052", "--aspect-ratio", "auto",
                            "--workdir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["current"]["aspect_ratio"] == "auto"
    assert {o["value"] for o in data["output"]} == {"480p", "720p", "1080p"}


def test_gen_edit_template_is_available_and_needs_its_variables():
    from vrgen.paths import templates_dir
    from vrgen.prep.prompt import template_placeholders
    text = (templates_dir() / "gen-edit.md").read_text(encoding="utf-8")
    keys = set(template_placeholders(text))
    for key in ("subject", "target", "plot", "motion_rule", "preserve",
                "allowed_changes"):
        assert key in keys
    assert "theme" not in keys


def test_character_repaint_template_is_available():
    from vrgen.paths import templates_dir
    from vrgen.prep.prompt import template_placeholders
    text = (templates_dir() / "character-repaint.md").read_text(encoding="utf-8")
    keys = set(template_placeholders(text))
    for key in ("subject", "target", "plot", "motion_rule", "preserve",
                "allowed_changes", "canvas_w", "canvas_h", "asset_list"):
        assert key in keys
    assert "最后一张图片是编辑区域标注" in text


def test_effective_aspect_maps_modes_with_a_ratio_list():
    from vrgen.prep.params import effective_aspect
    assert effective_aspect("auto", ()) == "auto"
    assert effective_aspect("auto", ("16:9", "21:9")) == "16:9"
    assert effective_aspect("21:9", ("16:9", "21:9")) == "21:9"


def test_params_save_persists_mode_and_aspect(tmp_path, monkeypatch):
    refs = _params_offline(tmp_path, monkeypatch)
    wd = str(tmp_path)
    r = runner.invoke(app, ["params", "save", "--job", "j1",
                            "--resolution", "1080p", "--scale", "auto",
                            "--mode", "allMediaRef", "--aspect-ratio", "16:9",
                            "--workdir", wd])
    assert r.exit_code == 0, r.output
    echo = json.loads(r.output)
    assert echo["mode"] == "allMediaRef" and echo["aspect_ratio"] == "16:9"
    text = jobs.generation_params_path(tmp_path, "j1").read_text(encoding="utf-8")
    assert 'mode = "allMediaRef"' in text and 'aspect_ratio = "16:9"' in text


def test_params_save_rejects_values_outside_the_ui_set(tmp_path):
    for args in (["--resolution", "2K", "--scale", "auto"],
                 ["--resolution", "720p", "--scale", "quarter"]):
        r = runner.invoke(app, ["params", "save", "--job", "j1", *args,
                                "--workdir", str(tmp_path)])
        assert r.exit_code == 2, r.output


def test_360_template_renders_plot_and_allowed_changes():
    from vrgen.paths import templates_dir
    from vrgen.prep.prompt import geometry_variables, render
    from vrgen.prep.scale import scaled_geometry

    template = (templates_dir() / "360-closure.md").read_text(encoding="utf-8")
    geom = scaled_geometry((4668, 2000), (4000, 2000), (4400, 1884))
    text = render(template, {
        **geometry_variables(geom),
        "asset_list": "@Video1：编辑母版，负责时间轴、构图与动作基准。",
        "theme": "黄昏下的木质帆船", "style": "电影级写实",
        "plot": "帆船沿原航向前进", "action": "船帆随风轻微摆动",
        "allowed_changes": "无。",
    })
    assert "输入文件为 4400×1884" in text
    assert "【情节与动作】" in text and "帆船沿原航向前进" in text
    assert "【允许变化】" in text and "无。" in text
    assert "输出像素尺寸由平台分辨率档决定" in text


