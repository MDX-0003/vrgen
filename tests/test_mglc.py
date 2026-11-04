"""mglc 后端迁入（← vrpad aigc）的测试迁移版。

原文件的两条铁律不变：**不碰网络/真 mglc**（runner/fetcher 注入）；
**退出码不是错误信号**（code 决定成败）。删掉的部分：case 发现（vrgen 显式入参
取代）、几何闸门（D3：归 vrstudio）。pipeline/CLI 测试改为 InputPackage 形态。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

import vrgen.cli as vrgen_cli
import vrgen.round.pipeline as pipe
from vrgen import config as config_mod
from vrgen import manual
from vrgen.config import ConfigError
from vrgen.errors import AigcError, BudgetError, LedgerError, TaskFailed, UsageError
from vrgen.platform.client import Envelope, Mglc
from vrgen.platform.models import find_model, load_models, parse_models
from vrgen.prep.budget import InputSpec, check_input, suggest_size
from vrgen.prep.prompt import geometry_variables, render
from vrgen.prep.scale import choose_upload_size, parse_size_mode, scaled_geometry
from vrgen.progress import Log
from vrgen.refs import RefAsset
from vrgen.round import task as task_mod
from vrgen.round.assets import asset_state, classify, ensure_assets, extract_asset_id, status_of
from vrgen.round.ledger import RunStore, assert_spendable, run_key
from vrgen.round.pipeline import InputPackage

V25_VIDEOEDIT = {
    "minCount": 1, "maxCount": 10, "formats": ["mp4"],
    "fps": {"min": 24, "max": 60}, "maxBytes": 209715200,
    "duration": {"min": 2, "max": 30}, "totalDuration": 30,
    "refVideoMinAspectRatio": 0.4, "refVideoMaxAspectRatio": 2.5,
    "minPixel": 409600, "maxPixel": 8295044,
}

CANVAS = (4668, 2000)
SOURCE = (4000, 2000)

# --------------------------------------------------------------- prep: budget


def test_padded_canvas_fails_both_budgets():
    spec = InputSpec(width=4668, height=2000, fps=30.0, duration_s=7.979,
                     size_bytes=228_200_000, container=("mp4",))
    report = check_input(spec, V25_VIDEOEDIT)
    assert not report.ok
    assert {c.label for c in report.failures} == {"pixels", "size"}
    width, height = report.suggested_size()
    assert width * height <= V25_VIDEOEDIT["maxPixel"]
    assert width % 2 == 0 and height % 2 == 0
    with pytest.raises(BudgetError):
        report.raise_if_bad()


def test_suggest_size_keeps_the_aspect_and_stays_even():
    spec = InputSpec(width=4668, height=2000, fps=30.0, duration_s=8.0,
                     size_bytes=228_200_000, container=("mp4",))
    width, height = suggest_size(spec, 8_295_044, 209_715_200)
    assert width % 2 == 0 and height % 2 == 0
    assert abs(width / height - spec.aspect) < 0.01


# ---------------------------------------------------------------- prep: scale


def test_same_geometry_at_every_scale():
    full = scaled_geometry(CANVAS, SOURCE, CANVAS)
    half = scaled_geometry(CANVAS, SOURCE, (2334, 1000))
    assert full.band_w == 668 and full.P == 334      # == vrpad manifest 的 L/P（668/334）
    assert half.band_w == 334 and half.P == 167
    assert half.region_main == (334, 2000)
    assert half.region_right == (2000, 2334)
    assert full.region_left[0] == half.region_left[0] == 0


def test_choose_upload_size_modes():
    spec = InputSpec(width=4668, height=2000, fps=30.0, duration_s=8.0,
                     size_bytes=228_200_000, container=("mp4",))
    assert choose_upload_size(CANVAS, mode="full") == CANVAS
    assert choose_upload_size(CANVAS, mode="half") == (2334, 1000)
    auto = choose_upload_size(CANVAS, mode="auto", spec=spec,
                              max_pixels=V25_VIDEOEDIT["maxPixel"],
                              max_bytes=V25_VIDEOEDIT["maxBytes"])
    assert auto[0] * auto[1] <= V25_VIDEOEDIT["maxPixel"]
    assert auto[0] < CANVAS[0] and auto[0] % 2 == 0


@pytest.mark.parametrize("text", ["4201x1800", "4200", "2K", "4200x1799"])
def test_parse_size_mode_rejects_junk(text):
    with pytest.raises(AigcError):
        parse_size_mode(text)


def test_prompt_numbers_follow_the_upload_scale():
    geom = scaled_geometry(CANVAS, SOURCE, (2334, 1000))
    variables = {**geometry_variables(geom), "theme": "黄昏"}
    text = render("canvas {canvas_w}x{canvas_h}; left {left_first}-{left_last}; "
                  "right {right_first}-{right_last}; main {main_w}; {theme}", variables)
    assert "canvas 2334x1000" in text
    assert "left 0-333" in text and "right 2000-2333" in text
    assert "main 1666" in text and "黄昏" in text


def test_unknown_placeholder_is_an_error_not_an_empty_string():
    with pytest.raises(AigcError) as excinfo:
        render("hello {nope}", {"canvas_w": 10})
    assert "nope" in str(excinfo.value)
    assert "canvas_w" in str(excinfo.value)


# ---------------------------------------------------------------- platform


class _Proc:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = ""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


def _runner(payload: dict | str, returncode: int = 0, calls: list | None = None):
    def run(argv, timeout):
        if calls is not None:
            calls.append(list(argv))
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return _Proc(text, returncode)
    return run


def test_exit_code_is_not_an_error_signal():
    client = Mglc(binary="mglc-fake", runner=_runner({"code": 0, "data": {"ok": 1}},
                                                     returncode=1))
    assert client.call("anything").ok


def test_not_logged_in_is_its_own_exception():
    from vrgen.errors import NotLoggedIn
    client = Mglc(binary="mglc-fake",
                  runner=_runner({"code": 10002, "data": None, "msg": "未登录"}))
    with pytest.raises(NotLoggedIn):
        client.status()


def test_generate_refuses_without_confirmation():
    from vrgen.errors import MglcError
    calls: list[list[str]] = []
    client = Mglc(binary="mglc-fake",
                  runner=_runner({"code": 0, "data": {"taskId": 7}}, calls=calls))
    with pytest.raises(MglcError) as excinfo:
        client.generate(["--prompt", "x"], confirm=False)
    assert "confirm" in str(excinfo.value) and "--yes" in str(excinfo.value)
    assert calls == []
    client.generate(["--prompt", "x"], confirm=True)
    assert calls and calls[0][1:3] == ["video", "generate"]


def test_model_list_parses_the_seedance_shape():
    payload = {"code": 0, "data": {"items": [{
        "code": 37, "displayName": "Seedance 2.5",
        "modelConfig": {
            "maxPromptLength": 10000,
            "referenceModes": [
                {"mode": "videoEdit", "supportVirtualIp": True, "maxCount": 10,
                 "resolutions": ["480p", "720p", "1080p"], "duration": {"type": ""},
                 "refVideoValidator": V25_VIDEOEDIT},
                {"mode": "firstFrame", "supportVirtualIp": False, "maxCount": 1},
            ]}}]}}
    models = parse_models(payload["data"]["items"])
    model = find_model(models, 37)
    assert model.name == "Seedance 2.5"
    assert model.video_input_modes == ("videoEdit",)
    assert model.mode("videoEdit").support_virtual_ip is True
    assert model.mode("firstFrame").takes_video is False


def test_find_model_prefers_the_full_entry_over_the_sample_variant():
    def raw(name, resolutions):
        return {"code": 37, "displayName": name, "modelConfig": {
            "referenceModes": [{"mode": "videoEdit", "supportVirtualIp": True,
                                "resolutions": resolutions,
                                "refVideoValidator": V25_VIDEOEDIT}]}}

    models = parse_models([raw("Seedance 2.5（样片模式）", ["480p"]),
                           raw("Seedance 2.5", ["480p", "720p", "1080p"])])
    model = find_model(models, 37)
    assert model.name == "Seedance 2.5"
    assert model.mode("videoEdit").resolutions == ("480p", "720p", "1080p")


def test_model_cache_is_used_and_refreshed(tmp_path):
    calls: list[str] = []

    def fetcher(url, timeout):
        calls.append(url)
        return [{"code": 37, "displayName": "Seedance 2.5",
                 "modelConfig": {"referenceModes": []}}]

    first = load_models(tmp_path, fetcher=fetcher)
    assert len(calls) == 1 and first[0].code == 37
    load_models(tmp_path, fetcher=fetcher)
    assert len(calls) == 1
    load_models(tmp_path, fetcher=fetcher, refresh=True)
    assert len(calls) == 2


# ------------------------------------------------------------------ ledger


def test_run_key_is_stable_and_sensitive(tmp_path):
    video, mask = tmp_path / "v.mp4", tmp_path / "m.png"
    video.write_bytes(b"video-bytes")
    mask.write_bytes(b"mask-bytes")
    params = {"model_id": 37, "mode": "videoEdit", "resolution": "720p"}

    a = run_key("prompt text", video, mask, params)
    assert a == run_key("prompt text", video, mask, params)
    assert a == run_key("prompt text  \n", video, mask, params)
    assert a != run_key("prompt text!", video, mask, params)
    assert a != run_key("prompt text", video, mask, {**params, "resolution": "1080p"})
    video.write_bytes(b"different")
    current = run_key("prompt text", video, mask, params)
    assert a != current
    assert current != run_key("prompt text", video, mask, params,
                              extra={"refs": [["v1", "abc"]]})
    assert current == run_key("prompt text", video, mask, params, extra={})


def test_run_store_round_trip_and_duplicate_refusal(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"v")
    store = RunStore(tmp_path / "runs")
    key = run_key("p", video, None, {"model_id": 37})
    run = store.create(key, params={"model_id": 37}, prompt="p", upload=video)
    run.task_id = 4242
    run.save()
    again = store.open(run.dir)
    assert again.key == key and again.task_id == 4242
    assert store.open(key[:12]).dir == run.dir
    with pytest.raises(LedgerError):
        store.create(key, params={}, prompt="p")


def test_spend_guard_blocks_a_second_submission(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"v")
    store = RunStore(tmp_path / "runs")
    run = store.create(run_key("p", video, None, {}), params={}, prompt="p", upload=video)
    assert_spendable(run)
    run.task_id = 99
    with pytest.raises(LedgerError) as excinfo:
        assert_spendable(run)
    assert "99" in str(excinfo.value)
    assert_spendable(run, force=True)
    assert "resubmitted" in run.notes[-1]


# ------------------------------------------------------------- round: task


def test_build_generate_argv_requires_asset_ids():
    common = dict(prompt="p", model_id=37, mode="videoEdit", duration_s=8,
                  resolution="720p", aspect_ratio="21:9", num=1)
    with pytest.raises(AigcError):
        task_mod.build_generate_argv(**common, videos=[("up.mp4", None)])
    argv = task_mod.build_generate_argv(
        **common, videos=[("up.mp4", 11)], images=[("mask.png", 22)])
    joined = " ".join(argv)
    assert "--type videoEdit" in joined
    assert "--video-asset-id 11" in joined and "--asset-id 22" in joined
    assert "--keep-original-sound" in joined
    preview = task_mod.build_generate_argv(
        **common, videos=[("up.mp4", None)], preview=True)
    assert "--video-asset-id <video-asset-id>" in " ".join(preview)


def test_build_generate_argv_auto_aspect_omits_the_flag():
    """videoEdit 的 ratios 为空、输出跟随输入；auto 表示不向 mglc 传该 flag。"""
    argv = task_mod.build_generate_argv(
        prompt="p", model_id=37, mode="videoEdit", duration_s=13,
        resolution="1080p", aspect_ratio="auto", num=1,
        videos=[("up.mp4", 11)])
    assert "--aspect-ratio" not in argv

    argv = task_mod.build_generate_argv(
        prompt="p", model_id=37, mode="videoEdit", duration_s=13,
        resolution="1080p", aspect_ratio="21:9", num=1,
        videos=[("up.mp4", 11)])
    assert "--aspect-ratio" in argv and "21:9" in argv


def test_build_generate_argv_carries_multiple_refs():
    argv = task_mod.build_generate_argv(
        prompt="p", model_id=37, mode="allMediaRef", duration_s=8,
        resolution="1080p", aspect_ratio="16:9", num=1,
        videos=[("v1.mp4", 11), ("v2.mp4", 12)],
        images=[("i1.png", 21), ("i2.png", 22)])
    joined = " ".join(argv)
    assert joined.count("--video ") == 2 and joined.count("--video-asset-id") == 2
    assert joined.count("--image ") == 2 and joined.count("--asset-id") == 2
    assert argv.index("v2.mp4") > argv.index("v1.mp4")
    assert argv.index("v1.mp4") < argv.index("11")


def test_build_generate_argv_first_last_frame():
    argv = task_mod.build_generate_argv(
        prompt="p", model_id=37, mode="firstLastFrame", duration_s=13,
        resolution="1080p", aspect_ratio="16:9", num=1,
        first_image=("first.png", None), last_image=("last.png", None))
    joined = " ".join(argv)
    assert "--first-image first.png" in joined
    assert "--last-image last.png" in joined
    assert argv.index("first.png") < argv.index("last.png")
    for flag in ("--video", "--image", "--video-asset-id", "--asset-id",
                 "--aspect-ratio", "--keep-original-sound"):
        assert flag not in joined


def test_build_generate_argv_requires_any_reference():
    with pytest.raises(AigcError, match="at least one reference"):
        task_mod.build_generate_argv(
            prompt="p", model_id=37, mode="allMediaRef", duration_s=8,
            resolution="1080p", aspect_ratio="16:9", num=1,
            videos=[], images=[])


def test_build_generate_argv_passes_project_session_id():
    argv = task_mod.build_generate_argv(
        prompt="p", model_id=37, mode="videoEdit", duration_s=13,
        resolution="1080p", aspect_ratio="auto", num=1,
        videos=[("up.mp4", 11)], session_id=2060150528006080)
    joined = " ".join(argv)
    assert "--session-id 2060150528006080" in joined

    argv = task_mod.build_generate_argv(
        prompt="p", model_id=37, mode="videoEdit", duration_s=13,
        resolution="1080p", aspect_ratio="auto", num=1,
        videos=[("up.mp4", 11)], session_id=0)
    assert "--session-id" not in argv


def test_task_failure_carries_the_platform_reason():
    """平台把具体原因放在 failReason；它必须出现在 TaskFailed 的消息里。"""

    def runner(argv, timeout):
        return _Proc2(json.dumps({"code": 0, "data": {
            "failReason": "所选任务类型与提示词/输入不匹配（视频编辑被判定为参考生成）",
            "status": 3, "taskId": 1}}))

    client = Mglc(binary="mglc-fake", runner=runner)
    with pytest.raises(TaskFailed) as excinfo:
        task_mod.wait(client, 1, interval=0.0, timeout=5)
    assert "视频编辑被判定为参考生成" in str(excinfo.value)
    assert excinfo.value.reason.startswith("所选任务类型")


def test_failure_reason_tolerates_other_field_names():
    assert task_mod.failure_reason({"msg": "OK", "data": {"errorMsg": "超时"}}) == "超时"
    assert task_mod.failure_reason({"data": {"status": 3, "msg": "OK"}}) == ""


def test_execute_round_notes_the_platform_reason(tmp_path, monkeypatch):
    conf, pkg, prepared, client = _fake_round(monkeypatch, tmp_path)

    def boom(*args, **kwargs):
        raise TaskFailed(7, "failed", reason="所选任务类型与提示词/输入不匹配")

    monkeypatch.setattr(pipe, "wait_task", boom)
    with pytest.raises(TaskFailed):
        pipe.execute_round(conf, pkg, prepared, client=client, log=Log(quiet=True))
    blob = json.loads((prepared.run.dir / "run.json").read_text(encoding="utf-8"))
    assert any("所选任务类型与提示词/输入不匹配" in note for note in blob["notes"])
    assert blob["stage"] == "failed"


def test_task_id_and_state_extraction_tolerate_nesting():
    assert task_mod.extract_task_id({"taskId": 5}) == 5
    assert task_mod.extract_task_id({"results": [{"taskAssetIds": [1], "taskId": 8}]}) == 8
    assert task_mod.parse_state({"data": {"state": 1}}) == (1, "running")
    assert task_mod.parse_state({"generateStatus": {"status": 2}}) == (2, "succeeded")


def test_result_urls_prefer_the_unwatermarked_render():
    payload = {"data": {"videos": [{"wmVideoUrl": "https://x/a_wm.mp4",
                                    "videoUrl": "https://x/a.mp4"}]}}
    urls = task_mod.result_urls(payload)
    assert urls[0] == "https://x/a.mp4"


def test_result_urls_accepts_image_suffix():
    payload = {"data": {"imageUrl": "https://x/a.png",
                        "wmImageUrl": "https://x/a_wm.png",
                        "imgUrlList": ["https://x/b.jpg"]}}
    urls = task_mod.result_urls(payload)
    assert urls[0] == "https://x/a.png"
    assert "https://x/b.jpg" in urls
    assert urls.index("https://x/a_wm.png") > urls.index("https://x/a.png")
    only_plain = task_mod.result_urls(payload, include_marked=False)
    assert "https://x/a_wm.png" not in only_plain
    assert "https://x/b.jpg" in only_plain


# ------------------------------------------------------------- round: assets


class _Proc2:
    def __init__(self, stdout: str, returncode: int = 0):
        self.stdout, self.returncode, self.stderr = stdout, returncode, ""


def _dispatch(calls: list | None = None, *, stale_ids: tuple[int, ...] = ()):
    def run(argv, timeout):
        joined = " ".join(argv)
        if calls is not None:
            calls.append(joined)
        if "virtual-ip info" in joined:
            asset = int(joined.split("info")[-1].strip())
            if asset in stale_ids:
                return _Proc2(json.dumps({"code": -400,
                                          "msg": "第1个资产不存在或不属于当前企业"}))
            return _Proc2(json.dumps({"code": 0, "data": {"status": "active"}}))
        if "virtual-ip upload" in joined:
            asset = 22 if "--type image" in joined else 11
            return _Proc2(json.dumps({"code": 0, "data": {"assetId": asset}}))
        if "video generate" in joined:
            return _Proc2(json.dumps({"code": 0, "data": {"taskId": 7}}))
        if "task status" in joined:
            return _Proc2(json.dumps({"code": 0, "data": {
                "status": 2,
                "videos": [{"videoUrl": "https://example.invalid/a.mp4",
                            "wmVideoUrl": "https://example.invalid/a_wm.mp4"}]}}))
        return _Proc2(json.dumps({"code": 0, "data": {}}))
    return run


def _asset_run(tmp_path, asset_id: int | None = 11):
    video = tmp_path / "up.mp4"
    video.write_bytes(b"v")
    store = RunStore(tmp_path / "runs")
    run = store.create(run_key("p", video, None, {}), params={}, prompt="p", upload=video)
    run.assets = {"video:v1": asset_id} if asset_id else {}
    run.save()
    return run


def test_recorded_asset_is_verified_and_replaced_when_foreign(tmp_path):
    calls: list[str] = []
    run = _asset_run(tmp_path, asset_id=99)
    client = Mglc(binary="mglc-fake", runner=_dispatch(calls, stale_ids=(99,)))
    ids = ensure_assets(client, run,
                        refs=[("video:v1", "video", run.upload["path"])],
                        poll=False, log=Log(quiet=True))
    assert ids["video:v1"] == 11
    assert any("no longer usable" in n for n in run.notes)


def test_recorded_asset_that_is_still_valid_is_not_re_uploaded(tmp_path):
    calls: list[str] = []
    run = _asset_run(tmp_path, asset_id=99)
    client = Mglc(binary="mglc-fake", runner=_dispatch(calls))
    ids = ensure_assets(client, run,
                        refs=[("video:v1", "video", run.upload["path"])],
                        poll=False, log=Log(quiet=True))
    assert ids["video:v1"] == 99
    assert not any("virtual-ip upload" in c for c in calls)


def test_an_unreadable_review_status_is_not_mistaken_for_pending(tmp_path):
    calls: list[str] = []
    run = _asset_run(tmp_path, asset_id=99)

    def runner(argv, timeout):
        joined = " ".join(argv)
        calls.append(joined)
        if "virtual-ip info" in joined:
            asset = int(joined.split("info")[-1].strip())
            if asset == 99:
                return _Proc2(json.dumps({"code": 0, "data": {"assetId": 99}}))
            return _Proc2(json.dumps({"code": 0, "data": {"status": "active"}}))
        if "virtual-ip upload" in joined:
            return _Proc2(json.dumps({"code": 0, "data": {"assetId": 11}}))
        return _Proc2(json.dumps({"code": 0, "data": {}}))

    client = Mglc(binary="mglc-fake", runner=runner)
    assert classify("") == "unknown"
    assert asset_state(client, 99) == "unreadable"
    ids = ensure_assets(client, run,
                        refs=[("video:v1", "video", run.upload["path"])],
                        poll=False, log=Log(quiet=True))
    assert ids["video:v1"] == 11


def test_extract_asset_id_and_status_readers():
    assert extract_asset_id({"data": {"assetId": 77}}) == 77
    assert extract_asset_id({"result": {"portraitAssetId": "88"}}) == 88
    assert status_of({"auditStatus": "Active"}) == "active"
    assert classify("processing") == "pending"
    with pytest.raises(AigcError):
        extract_asset_id({"nothing": "useful"})


# ------------------------------------------------------------ round: pipeline

_TEMPLATE = ("canvas {canvas_w}x{canvas_h}; band {band_w}; "
             "left {left_first}-{left_last}; {theme}")


def _model_cache(workdir: Path, *, max_pixel: int = 8_295_044) -> None:
    validator = dict(V25_VIDEOEDIT, maxPixel=max_pixel)
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "models.json").write_text(json.dumps({
        "fetched_at": time.time(),
        "items": [{"code": 37, "displayName": "Seedance 2.5",
                   "modelConfig": {"maxPromptLength": 10000, "referenceModes": [
                       {"mode": "videoEdit", "supportVirtualIp": True, "maxCount": 10,
                        "resolutions": ["480p", "720p", "1080p"],
                        "duration": {"min": 4, "max": 30},
                        "refVideoValidator": validator}]}}],
    }), encoding="utf-8")


def _offline(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    """模板指到 tmp；probe/scale 全离线。返回 (母版视频, mask)。"""
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "t.md").write_text(_TEMPLATE, encoding="utf-8")
    monkeypatch.setattr(pipe, "templates_dir", lambda: templates)
    monkeypatch.setattr(vrgen_cli, "templates_dir", lambda: templates)

    def fake_probe(path, conf):
        name = Path(path).name
        if name.startswith("upload_"):
            width, height = (int(p) for p in
                             name[len("upload_"):].split(".")[0].rsplit("_", 1)[1].split("x"))
            return InputSpec(width=width, height=height, fps=30.0, duration_s=7.979,
                             size_bytes=7_700_000, container=("mp4",))
        return InputSpec(width=CANVAS[0], height=CANVAS[1], fps=30.0, duration_s=7.979,
                         size_bytes=228_200_000, container=("mp4",))

    def fake_probe_image(path, conf):
        name = Path(path).name
        if name.startswith("upload_"):
            width, height = (int(p) for p in
                             name[len("upload_"):].split(".")[0].rsplit("_", 1)[1].split("x"))
        else:
            width, height = CANVAS
        return InputSpec(width=width, height=height, fps=0.0, duration_s=0.0,
                         size_bytes=100_000, container=("png",))

    monkeypatch.setattr(pipe, "probe_input", fake_probe)
    monkeypatch.setattr(pipe, "probe_image", fake_probe_image)

    def fake_scale(src, dst, size, **kw):
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_bytes(b"scaled")
        return Path(dst)

    monkeypatch.setattr(pipe, "scale_video", fake_scale)
    monkeypatch.setattr(pipe, "scale_image", fake_scale)
    monkeypatch.setattr(pipe, "scale_reference_image", fake_scale)

    video = tmp_path / "canvas.mp4"
    video.write_bytes(b"canvas-bytes")
    mask = tmp_path / "mask.png"
    mask.write_bytes(b"mask-bytes")
    return video, mask


def _pkg(tmp_path: Path, video: Path, mask: Path | None, workdir: Path) -> InputPackage:
    refs = [RefAsset("clip", "video", "edit_master", video)]
    if mask is not None:
        refs.append(RefAsset("mask", "image", "mask", mask))
    return InputPackage(job="j", canvas_size=CANVAS, source_size=SOURCE,
                        workdir=workdir / "j", template="t",
                        variables={"theme": "黄昏"}, refs=tuple(refs))


def test_prepare_round_rejects_resolution_the_model_does_not_offer(tmp_path, monkeypatch):
    conf = config_mod.load(overrides=["mglc.resolution=4K"])
    video, mask = _offline(monkeypatch, tmp_path)
    runs = tmp_path / "runs"
    _model_cache(runs / "j")                 # videoEdit 只发布 480p/720p/1080p
    with pytest.raises(UsageError, match="只支持这些输出档"):
        pipe.prepare_round(conf, _pkg(tmp_path, video, mask, runs),
                           log=Log(quiet=True))


def test_prepare_round_reads_the_constants_from_config(tmp_path, monkeypatch):
    conf = config_mod.load(overrides=["mglc.scale=half", "mglc.resolution=480p"])
    video, mask = _offline(monkeypatch, tmp_path)
    runs = tmp_path / "runs"
    _model_cache(runs / "j")
    prepared = pipe.prepare_round(conf, _pkg(tmp_path, video, mask, runs), log=Log(quiet=True))

    assert prepared.run.params["scale"] == "2334x1000"
    assert prepared.run.params["resolution"] == "480p"
    assert prepared.geometry.band_w == 334
    assert prepared.run.upload["width"] == 2334
    assert prepared.run.dir.name.startswith("t-")           # 以模板名命名
    assert "2334" in prepared.run.prompt
    assert "黄昏" in prepared.run.prompt
    refs = prepared.run.params["refs"]
    assert [r["id"] for r in refs] == ["clip", "mask"]
    assert refs[1]["role"] == "mask" and "input" not in prepared.run.params
    assert "mask" not in prepared.run.prompt             # mask 不参与编号
    assert (prepared.run.dir / "refs.json").is_file()


def test_prepare_round_audio_none_disables_keep_sound(tmp_path, monkeypatch):
    """--audio none：结果下载后去音轨，同时不再让平台保留原声。"""
    conf = config_mod.load(overrides=["mglc.audio=none"])
    video, mask = _offline(monkeypatch, tmp_path)
    runs = tmp_path / "runs"
    _model_cache(runs / "j")
    prepared = pipe.prepare_round(conf, _pkg(tmp_path, video, mask, runs), log=Log(quiet=True))

    assert prepared.run.params["audio"] == "none"
    assert prepared.run.params["keep_sound"] is False


def test_strip_audio_copies_video_and_drops_the_track(tmp_path, monkeypatch):
    """--audio none 的实现：只去音轨，不重编码视频流。"""
    import vrgen.platform.media as platform_media
    from vrgen.media import ProbeInfo

    src = tmp_path / "result.mp4"
    src.write_bytes(b"x")
    calls = []
    monkeypatch.setattr(platform_media, "ffmpeg", lambda config=None: "ffmpeg")
    monkeypatch.setattr(platform_media.videoio, "probe",
                        lambda path, config=None: ProbeInfo(
                            (2, 2), 30.0, 1, 1.0, has_audio=True))
    monkeypatch.setattr(platform_media, "_run",
                        lambda args, what: calls.append(args))
    monkeypatch.setattr(platform_media.os, "replace", lambda a, b: None)

    platform_media.strip_audio(src)

    args = calls[0]
    assert "-an" in args
    assert args[args.index("-c:v") + 1] == "copy"
    assert args[args.index("-map") + 1] == "0:v:0"


def test_strip_audio_is_a_noop_when_the_result_is_already_silent(tmp_path, monkeypatch):
    import vrgen.platform.media as platform_media
    from vrgen.media import ProbeInfo

    src = tmp_path / "result.mp4"
    src.write_bytes(b"x")
    calls = []
    monkeypatch.setattr(platform_media, "ffmpeg", lambda config=None: "ffmpeg")
    monkeypatch.setattr(platform_media.videoio, "probe",
                        lambda path, config=None: ProbeInfo(
                            (2, 2), 30.0, 1, 1.0, has_audio=False))
    monkeypatch.setattr(platform_media, "_run",
                        lambda args, what: calls.append(args))

    assert platform_media.strip_audio(src) == src
    assert calls == []


def test_prepare_round_audio_defaults_to_keep(tmp_path, monkeypatch):
    conf = config_mod.load()
    video, mask = _offline(monkeypatch, tmp_path)
    runs = tmp_path / "runs"
    _model_cache(runs / "j")
    prepared = pipe.prepare_round(conf, _pkg(tmp_path, video, mask, runs), log=Log(quiet=True))

    assert prepared.run.params["audio"] == "keep"
    assert prepared.run.params["keep_sound"] is True


def _model_cache_multi(workdir: Path) -> None:
    validator = dict(V25_VIDEOEDIT)
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "models.json").write_text(json.dumps({
        "fetched_at": time.time(),
        "items": [{"code": 37, "displayName": "Seedance 2.5",
                   "modelConfig": {"maxPromptLength": 10000, "referenceModes": [
                       {"mode": "videoEdit", "supportVirtualIp": True, "maxCount": 50,
                        "resolutions": ["1080p"], "ratios": [],
                        "duration": {"type": ""},
                        "refVideoValidator": validator,
                        "refImageValidator": {
                            "maxCount": 30, "formats": ["png", "jpg", "jpeg", "webp"],
                            "minPixel": 90000, "maxPixel": 36000000,
                            "maxBytes": 31457280,
                            "minAspectRatio": 0.4, "maxAspectRatio": 2.5}},
                       {"mode": "allMediaRef", "supportVirtualIp": True, "maxCount": 50,
                        "resolutions": ["1080p"], "ratios": ["16:9", "21:9"],
                        "duration": {"min": 4, "max": 30},
                        "refVideoValidator": validator,
                        "refImageValidator": {
                            "maxCount": 30, "formats": ["png", "jpg", "jpeg", "webp"],
                            "minPixel": 90000, "maxPixel": 36000000,
                            "maxBytes": 31457280,
                            "minAspectRatio": 0.4, "maxAspectRatio": 2.5}},
                       {"mode": "firstLastFrame", "supportVirtualIp": True, "maxCount": 2,
                        "resolutions": ["480p", "720p", "1080p"], "ratios": [],
                        "duration": {"type": "range", "min": 4, "max": 30},
                        "refImageValidator": {
                            "maxCount": 2, "formats": ["png", "jpg", "jpeg", "webp"],
                            "minPixel": 90000, "maxPixel": 36000000,
                            "maxBytes": 31457280,
                            "minAspectRatio": 0.4, "maxAspectRatio": 2.5}}]}}],
    }), encoding="utf-8")


def test_prepare_round_gen_multi_assets(tmp_path, monkeypatch):
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "t.md").write_text(
        "{asset_list}\n{task_rule}\n{timeline_rule}\ncanvas {canvas_w}x{canvas_h}",
        encoding="utf-8")
    monkeypatch.setattr(pipe, "templates_dir", lambda: templates)
    runs = tmp_path / "runs"
    _model_cache_multi(runs / "j")

    def fake_probe(path, conf):
        duration = 8.2 if Path(path).name == "v2.mp4" else 5.0
        return InputSpec(width=1920, height=1080, fps=30.0, duration_s=duration,
                         size_bytes=1_000_000, container=("mp4",))

    monkeypatch.setattr(pipe, "probe_input", fake_probe)
    monkeypatch.setattr(pipe, "probe_image", lambda path, conf: InputSpec(
        width=1000, height=1000, fps=0.0, duration_s=0.0,
        size_bytes=500_000, container=("png",)))

    def no_scale(*args, **kwargs):
        raise AssertionError("素材已经合格，不该缩放")

    monkeypatch.setattr(pipe, "scale_video", no_scale)
    monkeypatch.setattr(pipe, "scale_reference_image", no_scale)

    v1 = tmp_path / "v1.mp4"
    v2 = tmp_path / "v2.mp4"
    i1 = tmp_path / "i1.png"
    v1.write_bytes(b"v1")
    v2.write_bytes(b"v2")
    i1.write_bytes(b"i1")
    refs = (RefAsset("v1", "video", "edit_master", v1),
            RefAsset("v2", "video", "motion_ref", v2),
            RefAsset("i1", "image", "subject_ref", i1))
    pkg = InputPackage(job="j", canvas_size=None, source_size=None,
                       workdir=runs / "j", template="t",
                       variables={}, refs=refs)
    conf = config_mod.load(overrides=["mglc.mode=allMediaRef",
                                      "mglc.aspect_ratio=16:9"])
    prepared = pipe.prepare_round(conf, pkg, log=Log(quiet=True))

    assert prepared.geometry.canvas_size == (1920, 1080)
    assert prepared.geometry.band_w == 0
    assert prepared.run.params["duration_s"] == 9           # 最长视频 8.2s → 9
    assert prepared.run.params["duration_source"] == "longest"
    assert prepared.run.params["master"] == "v1"
    assert [r["id"] for r in prepared.run.params["refs"]] == ["v1", "v2", "i1"]
    assert prepared.run.upload["path"] == str(v1)
    assert "@Video1" in prepared.run.prompt
    assert "@Video2" in prepared.run.prompt
    assert "@Image1" in prepared.run.prompt
    assert "锁定参考素材的空间" in prepared.run.prompt
    assert "1920x1080" in prepared.run.prompt


def _firstlast_refs(tmp_path):
    i1 = tmp_path / "i1.png"
    i2 = tmp_path / "i2.png"
    i1.write_bytes(b"i1")
    i2.write_bytes(b"i2")
    return (
        RefAsset("i1", "image", "first_frame", i1),
        RefAsset("i2", "image", "last_frame", i2),
    )


def _firstlast_pkg(tmp_path, runs, refs):
    return InputPackage(job="j", canvas_size=None, source_size=None,
                        workdir=runs / "j", template="t",
                        variables={}, refs=refs)


def _firstlast_offline(tmp_path, monkeypatch, *, duration: int | None = None):
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "t.md").write_text(
        "{asset_list}\n{task_rule}\n{timeline_rule}\ncanvas {canvas_w}x{canvas_h}",
        encoding="utf-8")
    monkeypatch.setattr(pipe, "templates_dir", lambda: templates)
    runs = tmp_path / "runs"
    _model_cache_multi(runs / "j")
    monkeypatch.setattr(pipe, "probe_image", lambda path, conf: InputSpec(
        width=1920, height=1080, fps=0.0, duration_s=0.0,
        size_bytes=500_000, container=("png",)))

    def no_scale(*args, **kwargs):
        raise AssertionError("首尾帧已合格，不该缩放")

    monkeypatch.setattr(pipe, "scale_reference_image", no_scale)
    overrides = ["mglc.mode=firstLastFrame"]
    if duration is not None:
        overrides.append(f"mglc.duration_s={duration}")
    conf = config_mod.load(overrides=overrides)
    return conf, runs, _firstlast_refs(tmp_path)


def test_prepare_round_gen_image_only(tmp_path, monkeypatch):
    conf, runs, refs = _firstlast_offline(tmp_path, monkeypatch, duration=13)
    prepared = pipe.prepare_round(
        conf, _firstlast_pkg(tmp_path, runs, refs), log=Log(quiet=True))

    assert prepared.run.params["duration_s"] == 13
    assert prepared.run.params["duration_source"] == "none"
    assert prepared.run.params["keep_sound"] is False
    assert prepared.run.params["master"] == "i1"
    assert prepared.run.upload["path"] == str(refs[0].path)
    assert prepared.geometry.canvas_size == (1920, 1080)
    assert "首帧定稿图" in prepared.run.prompt
    assert "尾帧定稿图" in prepared.run.prompt


def test_prepare_round_first_last_defaults_to_four_seconds(tmp_path, monkeypatch):
    conf, runs, refs = _firstlast_offline(tmp_path, monkeypatch)
    prepared = pipe.prepare_round(
        conf, _firstlast_pkg(tmp_path, runs, refs), log=Log(quiet=True))
    assert prepared.run.params["duration_s"] == 4


def test_duration_explicit_out_of_range(tmp_path, monkeypatch):
    conf, runs, refs = _firstlast_offline(tmp_path, monkeypatch, duration=31)
    with pytest.raises(UsageError, match="4-30"):
        pipe.prepare_round(conf, _firstlast_pkg(tmp_path, runs, refs),
                           log=Log(quiet=True))


def test_prepare_round_rejects_two_edit_masters(tmp_path, monkeypatch):
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "t.md").write_text("{asset_list}", encoding="utf-8")
    monkeypatch.setattr(pipe, "templates_dir", lambda: templates)
    runs = tmp_path / "runs"
    _model_cache_multi(runs / "j")
    monkeypatch.setattr(pipe, "probe_input", lambda path, conf: InputSpec(
        width=1920, height=1080, fps=30.0, duration_s=5.0,
        size_bytes=1_000_000, container=("mp4",)))
    v1 = tmp_path / "v1.mp4"
    v2 = tmp_path / "v2.mp4"
    v1.write_bytes(b"v1")
    v2.write_bytes(b"v2")
    refs = (RefAsset("v1", "video", "edit_master", v1),
            RefAsset("v2", "video", "edit_master", v2))
    pkg = InputPackage(job="j", canvas_size=None, source_size=None,
                       workdir=runs / "j", template="t",
                       variables={}, refs=refs)
    conf = config_mod.load(overrides=["mglc.mode=allMediaRef"])
    with pytest.raises(UsageError, match="至多"):
        pipe.prepare_round(conf, pkg, log=Log(quiet=True))


def test_prepare_round_is_idempotent(tmp_path, monkeypatch):
    conf = config_mod.load(overrides=["mglc.scale=half"])
    video, mask = _offline(monkeypatch, tmp_path)
    runs = tmp_path / "runs"
    _model_cache(runs / "j")
    pkg = _pkg(tmp_path, video, mask, runs)
    first = pipe.prepare_round(conf, pkg, log=Log(quiet=True))
    again = pipe.prepare_round(conf, pkg, log=Log(quiet=True))
    assert again.reused is True and again.run.dir == first.run.dir
    assert len(list(runs.glob("j/*/run.json"))) == 1


def test_prepare_round_resubmit_opens_a_second_round(tmp_path, monkeypatch):
    """--resubmit 是另开一轮：同提示词、同素材，但目录与 key 都是新的。"""
    conf = config_mod.load(overrides=["mglc.scale=half"])
    video, mask = _offline(monkeypatch, tmp_path)
    runs = tmp_path / "runs"
    _model_cache(runs / "j")
    pkg = _pkg(tmp_path, video, mask, runs)
    first = pipe.prepare_round(conf, pkg, log=Log(quiet=True))
    forced = pipe.prepare_round(conf, pkg, log=Log(quiet=True),
                                resubmit_token="take-2")
    assert forced.reused is False
    assert forced.run.dir != first.run.dir
    assert forced.run.key != first.run.key
    assert any("resubmit" in note for note in forced.run.notes)
    assert len(list(runs.glob("j/*/run.json"))) == 2

    # 同一个 token 再来一次会命中它自己：resubmit 不是"每次调用都新开"。
    same = pipe.prepare_round(conf, pkg, log=Log(quiet=True), resubmit_token="take-2")
    assert same.reused is True and same.run.dir == forced.run.dir


def test_resubmit_refuses_a_round_that_is_still_in_flight(tmp_path, monkeypatch):
    conf = config_mod.load(overrides=["mglc.scale=half"])
    video, mask = _offline(monkeypatch, tmp_path)
    runs = tmp_path / "runs"
    _model_cache(runs / "j")
    pkg = _pkg(tmp_path, video, mask, runs)
    first = pipe.prepare_round(conf, pkg, log=Log(quiet=True))
    first.run.task_id = 7
    first.run.advance("submitted")
    first.run.save()
    with pytest.raises(LedgerError, match="still running"):
        pipe.prepare_round(conf, pkg, log=Log(quiet=True), resubmit_token="take-2")


def _fake_round(monkeypatch, tmp_path, *, calls: list | None = None):
    conf = config_mod.load(overrides=["mglc.scale=half"])
    video, mask = _offline(monkeypatch, tmp_path)
    runs = tmp_path / "runs"
    _model_cache(runs / "j")
    prepared = pipe.prepare_round(conf, _pkg(tmp_path, video, mask, runs), log=Log(quiet=True))

    def fake_fetch(client, run, *, timeout=600.0, log=None):
        target = run.dir / "result.mp4"
        target.write_bytes(b"model-return")
        run.result = str(target)
        run.advance("fetched")
        run.save()
        return target

    monkeypatch.setattr(pipe, "fetch_result", fake_fetch)
    monkeypatch.setattr(manual, "probe_gate",
                        lambda f: {"verdict": "ok", "width": 2334, "height": 1000,
                                   "frames": 239, "duration_s": 7.97})
    client = Mglc(binary="mglc-fake", runner=_dispatch(calls))
    return conf, _pkg(tmp_path, video, mask, runs), prepared, client


def test_execute_round_runs_to_the_end_and_records_everything(tmp_path, monkeypatch):
    calls: list[str] = []
    conf, pkg, prepared, client = _fake_round(monkeypatch, tmp_path, calls=calls)
    outcome = pipe.execute_round(conf, pkg, prepared, client=client, log=Log(quiet=True))

    assert outcome.submitted == 7
    assert outcome.run.stage == "checked"
    assert outcome.result is not None and Path(outcome.result).is_file()
    assert outcome.gate is not None and outcome.gate["verdict"] == "ok"   # 通用闸门
    joined = " | ".join(calls)
    assert "virtual-ip upload" in joined and "video generate" in joined
    assert len([c for c in calls if "video generate" in c]) == 1
    generate = [c for c in calls if "video generate" in c][0]
    assert "--video-asset-id 11" in generate and "--asset-id 22" in generate


def test_the_task_id_reaches_the_user_before_the_wait(tmp_path, monkeypatch):
    conf, pkg, prepared, client = _fake_round(monkeypatch, tmp_path)
    seen: list[int] = []
    pipe.execute_round(conf, pkg, prepared, client=client, log=Log(quiet=True),
                       on_submit=seen.append)
    assert seen == [7]


def test_execute_round_never_resubmits_a_submitted_task(tmp_path, monkeypatch):
    """断点续跑的核心断言：第二次跑不发新任务、不重复扣额度。"""
    calls: list[str] = []
    conf, pkg, prepared, client = _fake_round(monkeypatch, tmp_path, calls=calls)
    first = pipe.execute_round(conf, pkg, prepared, client=client, log=Log(quiet=True))
    assert first.submitted == 7

    again = pipe.prepare_round(conf, pkg, log=Log(quiet=True))      # 同 key 复用
    second = pipe.execute_round(conf, pkg, again, client=client, log=Log(quiet=True))

    assert second.submitted is None
    assert len([c for c in calls if "video generate" in c]) == 1
    assert second.run.task_id == 7
    assert second.result is not None


def test_execute_round_refuses_a_failed_round(tmp_path, monkeypatch):
    conf, pkg, prepared, client = _fake_round(monkeypatch, tmp_path)
    prepared.run.task_id = 9
    prepared.run.advance("failed")
    prepared.run.save()
    with pytest.raises(LedgerError) as excinfo:
        pipe.execute_round(conf, pkg, prepared, client=client, log=Log(quiet=True))
    assert "9" in str(excinfo.value) and "--resubmit" in str(excinfo.value)


def test_a_round_past_prepare_still_re_verifies_its_assets(tmp_path, monkeypatch):
    calls: list[str] = []
    conf, pkg, prepared, _ = _fake_round(monkeypatch, tmp_path, calls=calls)
    prepared.run.advance("assets_ready")
    prepared.run.assets = {"video:clip": 99, "image:mask": 98}
    prepared.run.save()

    client = Mglc(binary="mglc-fake", runner=_dispatch(calls, stale_ids=(98, 99)))
    outcome = pipe.execute_round(conf, pkg, prepared, client=client, log=Log(quiet=True))
    assert sum("virtual-ip upload" in c for c in calls) == 2
    assert outcome.run.assets["video:clip"] == 11
    assert outcome.run.assets["image:mask"] == 22


# ------------------------------------------------------- round: the call log


def test_call_log_appends_every_attempt(tmp_path):
    from vrgen.round.ledger import append_call_log
    video = tmp_path / "v.mp4"
    video.write_bytes(b"v")
    run = RunStore(tmp_path / "runs").create(run_key("p", video, None, {}),
                                             params={}, prompt="p", upload=video)
    good = Envelope(("mglc", "virtual-ip", "upload"), 0, {}, "", "",
                    "2026-09-23T10:00:00Z")
    bad = Envelope(("mglc", "video", "generate"), -400, None,
                   "第1个资产不存在或不属于当前企业", "", "2026-09-23T10:01:00Z")
    append_call_log(run, [good], mode="attempt 1")
    path = append_call_log(run, [bad], mode="attempt 2")
    text = path.read_text(encoding="utf-8")
    assert text.count("===") == 2
    assert "code=-400" in text and "不属于当前企业" in text
    # 空载荷不写 data 行（上传的信封 data={}）。
    assert "data:" not in text


def test_call_log_writes_the_reply_payload_with_signed_urls_trimmed(tmp_path):
    from vrgen.round.ledger import append_call_log
    video = tmp_path / "v.mp4"
    video.write_bytes(b"v")
    run = RunStore(tmp_path / "runs").create(run_key("p", video, None, {}),
                                             params={}, prompt="p", upload=video)
    signed = ("https://aigc-assets.mgtv.com/aigc/prod/2026/11/03/x.mp4"
              "?q-sign-algorithm=sha1&q-signature=deadbeef")
    envelope = Envelope(("mglc", "virtual-ip", "info", "1"), 0,
                        {"data": [{"assetId": 2067904785470336, "status": "active",
                                   "fileUrl": signed}], "msg": "OK"},
                        "OK", "", "2026-11-03T10:00:00Z")
    path = append_call_log(run, [envelope], mode="vrgen submit")
    text = path.read_text(encoding="utf-8")
    assert "    data: " in text
    assert '"assetId":2067904785470336' in text
    assert '"status":"active"' in text
    assert "x.mp4" in text and "q-signature" not in text


# ------------------------------------------------------------------ config


def test_mglc_defaults_are_pinned_to_the_locked_edit_path():
    conf = config_mod.load()
    assert conf.get("mglc.video_model_id") == 37
    assert conf.get("mglc.mode") == "videoEdit"
    assert conf.get("mglc.aspect_ratio") == "21:9"


def test_mglc_config_absorbs_legacy_model_key(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "vrgen.toml").write_text("[mglc]\nmodel_id = 22\n",
                                         encoding="utf-8")
    conf = config_mod.load()
    assert conf.get("mglc.video_model_id") == 22


@pytest.mark.parametrize("key,value", [
    ("mglc.video_model_id", "0"),
    ("mglc.resolution", '"9000p"'),
    ("mglc.num", "5"),
    ("mglc.audio", '"silent"'),
    ("mglc.scale", '"4201x1800"'),
    ("mglc.poll_interval_s", "0"),
    ("mglc.upload_crf", "99"),
])
def test_mglc_config_validation(key, value):
    with pytest.raises(ConfigError):
        config_mod.load(overrides=[f"{key}={value}"])


# --------------------------------------------------------------------- CLI


def _refs_file(tmp_path: Path, video: Path, mask: Path | None) -> Path:
    assets = [{"id": "clip", "kind": "video", "role": "edit_master",
               "path": str(video)}]
    if mask is not None:
        assets.append({"id": "mask", "kind": "image", "role": "mask",
                       "path": str(mask)})
    path = tmp_path / "refs.json"
    path.write_text(json.dumps({"assets": assets}, ensure_ascii=False), encoding="utf-8")
    return path


def test_cli_dry_run_needs_no_binary_and_sends_nothing(tmp_path, monkeypatch):
    video, mask = _offline(monkeypatch, tmp_path)
    runs = tmp_path / "runs"
    _model_cache(runs / "j")
    from vrgen import jobs as vrgen_jobs
    vrgen_jobs.save_prompt_vars(runs, "j", vrgen_jobs.PromptVars(vars={"theme": "黄昏"}))
    monkeypatch.setenv("VRGEN_MGLC_BIN", str(tmp_path / "definitely-absent.exe"))
    import vrgen.platform.binpath as binpath
    monkeypatch.setattr(binpath.shutil, "which", lambda name: None)

    result = CliRunner().invoke(vrgen_cli.app, [
        "submit", "--job", "j", "--refs", str(_refs_file(tmp_path, video, mask)),
        "--canvas", "4668x2000", "--source", "4000x2000",
        "--template", "t", "--dry-run", "--workdir", str(runs)])
    assert result.exit_code == 0, result.output
    assert "什么都没发" in result.output
    run = RunStore(runs / "j").all()[0]
    assert run.stage == "prepared" and run.task_id is None
    assert (run.dir / "mglc.log").is_file()
    assert "no external calls" in (run.dir / "mglc.log").read_text(encoding="utf-8")


def test_cli_dry_run_reads_scale_from_config(tmp_path, monkeypatch):
    video, mask = _offline(monkeypatch, tmp_path)
    runs = tmp_path / "runs"
    _model_cache(runs / "j")
    from vrgen import jobs as vrgen_jobs
    vrgen_jobs.save_prompt_vars(runs, "j", vrgen_jobs.PromptVars(vars={"theme": "黄昏"}))
    monkeypatch.setenv("VRGEN_MGLC_BIN", str(tmp_path / "definitely-absent.exe"))
    import vrgen.platform.binpath as binpath
    monkeypatch.setattr(binpath.shutil, "which", lambda name: None)
    cfg = tmp_path / "vrgen.toml"
    cfg.write_text('[mglc]\nscale = "half"\n', encoding="utf-8")

    result = CliRunner().invoke(vrgen_cli.app, [
        "submit", "--job", "j", "--refs", str(_refs_file(tmp_path, video, mask)),
        "--canvas", "4668x2000", "--source", "4000x2000",
        "--template", "t", "--dry-run", "--workdir", str(runs),
        "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "2334x1000" in result.output


def test_cli_submit_explicit_params_override_config(tmp_path, monkeypatch):
    video, mask = _offline(monkeypatch, tmp_path)
    runs = tmp_path / "runs"
    _model_cache(runs / "j")
    from vrgen import jobs as vrgen_jobs
    vrgen_jobs.save_prompt_vars(runs, "j", vrgen_jobs.PromptVars(vars={"theme": "黄昏"}))
    monkeypatch.setenv("VRGEN_MGLC_BIN", str(tmp_path / "definitely-absent.exe"))
    import vrgen.platform.binpath as binpath
    monkeypatch.setattr(binpath.shutil, "which", lambda name: None)
    cfg = tmp_path / "vrgen.toml"
    cfg.write_text('[mglc]\nresolution = "480p"\nscale = "auto"\n', encoding="utf-8")

    result = CliRunner().invoke(vrgen_cli.app, [
        "submit", "--job", "j", "--refs", str(_refs_file(tmp_path, video, mask)),
        "--canvas", "4668x2000", "--source", "4000x2000",
        "--template", "t", "--dry-run", "--workdir", str(runs),
        "--config", str(cfg),
        "--video-model-id", "37", "--mode", "videoEdit", "--resolution", "720p",
        "--duration", "9", "--num", "1", "--aspect-ratio", "21:9",
        "--scale", "half"])
    assert result.exit_code == 0, result.output
    run = RunStore(runs / "j").all()[0]
    assert run.params["resolution"] == "720p"
    assert run.params["scale"] == "2334x1000"
    assert run.params["duration_s"] == 9
    assert run.params["video_model_id"] == 37
    assert run.params["mode"] == "videoEdit"
    assert run.params["aspect_ratio"] == "21:9"

