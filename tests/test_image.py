"""Image round lifecycle and CLI surface (契约 case-model.md §4/§5)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from vrgen import cli as vrgen_cli
from vrgen import config as config_mod
from vrgen import jobs
from vrgen.errors import UsageError
from vrgen.paths import templates_dir
from vrgen.prep.prompt import template_aliases, template_help
from vrgen.progress import Log
from vrgen.round import image as image_mod
from vrgen.round.image import ImagePackage, prepare_image_round
from vrgen.round.ledger import RunStore
from vrgen.round.task import extract_task_ids

runner = CliRunner()


class _Proc:
    def __init__(self, payload: dict):
        self.stdout = json.dumps(payload)
        self.returncode = 0
        self.stderr = ""


def _image_runner(calls: list[str]):
    state = {"status_calls": 0}

    def run(argv, timeout):
        joined = " ".join(argv)
        calls.append(joined)
        if "image generate" in joined:
            return _Proc({"code": 0, "data": {"taskId": 7}})
        if "task status" in joined:
            state["status_calls"] += 1
            if state["status_calls"] == 1:
                return _Proc({"code": 0, "data": {"status": 1}})
            images = [{"imageUrl": f"https://example.invalid/{i}.png"}
                      for i in range(1, 5)]
            return _Proc({"code": 0, "data": {"status": 2, "images": images}})
        return _Proc({"code": 0, "data": {}})
    return run


class _Response:
    def __init__(self, data: bytes):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.data


def _images(tmp_path: Path, count: int = 2) -> tuple[Path, ...]:
    out = []
    for index in range(1, count + 1):
        path = tmp_path / f"i{index}.png"
        path.write_bytes(f"png-{index}".encode())
        out.append(path)
    return tuple(out)


def _conf(tmp_path: Path):
    return config_mod.load(overrides=["mglc.poll_interval_s=0.01",
                                      "mglc.poll_timeout_s=5"])


def _package(tmp_path: Path, images: tuple[Path, ...]) -> ImagePackage:
    return ImagePackage(job="j", prompt="把白膜换成人", model_id=44,
                        images=images, ratio="16:9", resolution="4K",
                        num=4, workdir=tmp_path / "runs" / "j")


def test_image_round_lifecycle(tmp_path, monkeypatch):
    images = _images(tmp_path)
    conf = _conf(tmp_path)
    pkg = _package(tmp_path, images)
    prepared = prepare_image_round(conf, pkg, log=Log(quiet=True))
    assert prepared.run.params["kind"] == "image"
    assert prepared.run.dir.name.startswith("img-")
    # the printed round is a directory name; `result --round` must accept it
    store = RunStore(prepared.run.dir.parent)
    assert store.open(prepared.run.dir.name).key == prepared.run.key

    from vrgen.platform.client import Mglc
    from vrgen.prep.budget import InputSpec

    monkeypatch.setattr(image_mod, "probe_image", lambda path, config=None: InputSpec(
        width=1920, height=1080, fps=0.0, duration_s=0.0,
        size_bytes=1000, container=("png",)))
    calls: list[str] = []
    downloads: list[str] = []

    def opener(url, timeout):
        downloads.append(url)
        return _Response(b"png-bytes")

    client = Mglc(binary="mglc-fake", runner=_image_runner(calls))
    outcome = image_mod.execute_image_round(
        conf, pkg, prepared, client=client, log=Log(quiet=True), opener=opener)

    assert len(outcome.results) == 4
    assert all(path.is_file() for path in outcome.results)
    assert outcome.run.results == [str(p) for p in outcome.results]
    assert outcome.run.stage == "checked"
    assert outcome.gate["verdict"] == "ok"
    assert len(downloads) == 4
    generate = [c for c in calls if "image generate" in c][0]
    assert "--ratio 16:9" in generate and "--resolution 4K" in generate
    assert generate.count("--image ") == 2


def test_extract_task_ids_reads_every_sibling():
    payload = {"data": [{"taskId": 11, "imageId": 12},
                        {"taskId": 13, "imageId": 14}]}
    assert extract_task_ids(payload) == [11, 13]
    assert extract_task_ids({"data": {"taskIds": [11, 13]}}) == [11, 13]


def test_image_batch_polls_and_downloads_every_task(tmp_path, monkeypatch):
    images = _images(tmp_path)
    conf = _conf(tmp_path)
    pkg = _package(tmp_path, images)
    prepared = prepare_image_round(conf, pkg, log=Log(quiet=True))
    from vrgen.platform.client import Mglc
    from vrgen.prep.budget import InputSpec

    monkeypatch.setattr(image_mod, "probe_image", lambda path, config=None: InputSpec(
        width=1920, height=1080, fps=0.0, duration_s=0.0,
        size_bytes=1000, container=("png",)))
    calls: list[str] = []

    def run(argv, timeout):
        joined = " ".join(argv)
        calls.append(joined)
        if "image generate" in joined:
            return _Proc({"code": 0, "data": {"tasks": [
                {"taskId": 11, "imageId": 12},
                {"taskId": 13, "imageId": 14}]}})
        if "task status" in joined:
            suffix = "a" if "--task-id 11" in joined else "b"
            return _Proc({"code": 0, "data": {"status": 2, "images": [
                {"imageUrl": f"https://example.invalid/{suffix}.png"}]}})
        return _Proc({"code": 0, "data": {}})

    def opener(url, timeout):
        return _Response(b"png-bytes")

    client = Mglc(binary="mglc-fake", runner=run)
    outcome = image_mod.execute_image_round(
        conf, pkg, prepared, client=client, log=Log(quiet=True), opener=opener)
    assert outcome.run.task_id == 11
    assert outcome.run.task_ids == [11, 13]
    assert len(outcome.results) == 2
    assert {path.name for path in outcome.results} == {"result_1.png", "result_2.png"}


def test_image_submit_dry_run(tmp_path):
    images = _images(tmp_path)
    result = runner.invoke(vrgen_cli.app, [
        "image", "submit", "--job", "j", "--prompt", "p",
        "--image", str(images[0]), "--image", str(images[1]),
        "--workdir", str(tmp_path / "runs"), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "什么都没发" in result.output
    run = RunStore(tmp_path / "runs" / "j").all()[0]
    assert run.params["kind"] == "image" and run.task_id is None
    assert "no external calls" in (run.dir / "mglc.log").read_text(encoding="utf-8")


def test_image_submit_model_prefers_flag_then_draft_then_config(tmp_path):
    images = _images(tmp_path)
    root = tmp_path / "runs"
    jobs.save_generation_params(root, "j", jobs.GenerationParams(image_model_id=25))

    def submitted_models(job: str) -> list[int]:
        return [run.params["image_model_id"]
                for run in RunStore(root / job).all()]

    common = ["--prompt", "p", "--image", str(images[0]), "--image", str(images[1]),
              "--workdir", str(root), "--dry-run"]
    result = runner.invoke(vrgen_cli.app, ["image", "submit", "--job", "j", *common])
    assert result.exit_code == 0, result.output
    assert submitted_models("j") == [25]

    result = runner.invoke(vrgen_cli.app, ["image", "submit", "--job", "j",
                                           "--image-model-id", "33", *common])
    assert result.exit_code == 0, result.output
    assert sorted(submitted_models("j")) == [25, 33]

    result = runner.invoke(vrgen_cli.app, ["image", "submit", "--job", "j2", *common])
    assert result.exit_code == 0, result.output
    assert submitted_models("j2") == [44]


def _seed_keyframe_vars(root: Path, step: str = "first") -> None:
    jobs.edit_prompt_vars(
        root, "j",
        sets={"target": "人物",
              "source_first": "底图是占位人形：只取位置与大小，不继承站姿",
              "source_last": "底图是占位人形：只取位置与大小，不继承站姿",
              "figure": "老年文官，黑冠红袍",
              "subject_last": "同一位老年文官，外观同首帧定稿",
              "pose_first": "坐在矮案后，面向画面右侧",
              "pose_last": "站在红毯中央，双手垂在身前",
              "preserve": "机位、场景与光照保持图1原样",
              "cleanup": ""},
        template=f"keyframe-{step}", vars_file="keyframe-vars.toml")


def test_image_submit_renders_template(tmp_path):
    images = _images(tmp_path)
    root = tmp_path / "runs"
    _seed_keyframe_vars(root)
    result = runner.invoke(vrgen_cli.app, [
        "image", "submit", "--job", "j",
        "--template", "keyframe-first", "--vars-file", "keyframe-vars.toml",
        "--step", "first",
        "--image-role", "first_frame_raw", "--image-role", "subject_ref",
        "--image", str(images[0]), "--image", str(images[1]),
        "--workdir", str(root), "--dry-run"])
    assert result.exit_code == 0, result.output
    run = RunStore(root / "j").all()[0]
    assert run.params["kind"] == "image"
    assert run.params["step"] == "first"
    prompt = run.prompt_path.read_text(encoding="utf-8")
    assert "老年文官" in prompt
    assert "图1 = 首帧原始图" in prompt
    assert "图2 = 人物三视图" in prompt
    assert "{" not in prompt and "|" not in prompt


def test_image_submit_rejects_prompt_and_template_together(tmp_path):
    images = _images(tmp_path)
    root = tmp_path / "runs"
    _seed_keyframe_vars(root)
    result = runner.invoke(vrgen_cli.app, [
        "image", "submit", "--job", "j", "--prompt", "p",
        "--template", "keyframe-first", "--vars-file", "keyframe-vars.toml",
        "--image-role", "first_frame_raw", "--image", str(images[0]),
        "--workdir", str(root), "--dry-run"])
    assert result.exit_code == 2


def test_image_submit_rejects_role_count_mismatch(tmp_path):
    images = _images(tmp_path)
    root = tmp_path / "runs"
    _seed_keyframe_vars(root)
    result = runner.invoke(vrgen_cli.app, [
        "image", "submit", "--job", "j",
        "--template", "keyframe-first", "--vars-file", "keyframe-vars.toml",
        "--image-role", "first_frame_raw",
        "--image", str(images[0]), "--image", str(images[1]),
        "--workdir", str(root), "--dry-run"])
    assert result.exit_code == 2


def test_image_roles_text_includes_notes():
    text = image_mod.image_roles_text(
        ["first_frame_raw", "subject_ref", "detail_ref"],
        ["", "", "头冠纹样与金色饰物。"])
    assert "图1 = 首帧原始图（底图：场景、机位与对象位置）。" in text
    assert "图2 = 人物三视图（唯一外观依据）。" in text
    assert ("图3 = 细节参考图（只继承所指细节的外观，不取构图、背景与姿态）"
            "：头冠纹样与金色饰物。") in text


def test_image_roles_text_covers_still_roles():
    text = image_mod.image_roles_text(["edit_master", "mask"])
    assert "图1 = 待重绘的 360 底图（本轮唯一要修改的底图）。" in text
    assert "图2 = 掩码图（黑色区域需要重绘，白色区域保持原样）。" in text


def test_image_roles_text_covers_lighting_ref():
    text = image_mod.image_roles_text(["edit_master", "mask", "lighting_ref"])
    assert "图3 = 氛围与光影参考图" in text
    assert "不取构图" in text
    assert "图3 = lighting_ref。" not in text


def test_full_redraw_img_template_renders(tmp_path):
    images = _images(tmp_path)
    root = tmp_path / "runs"
    jobs.edit_prompt_vars(
        root, "j",
        sets={"edit": "把整座宫殿改成烛火通明、暖色高光的夜宴氛围",
              "preserve": "梁柱结构、陈设位置与构图不动"},
        template="fullRedrawImg", vars_file="still-vars.toml")
    result = runner.invoke(vrgen_cli.app, [
        "image", "submit", "--job", "j",
        "--template", "fullRedrawImg", "--vars-file", "still-vars.toml",
        "--image-role", "edit_master", "--image-role", "mask",
        "--image-role", "lighting_ref",
        "--image", str(images[0]), "--image", str(images[1]),
        "--image", str(images[0]),
        "--canvas", "9558x4096", "--source", "8192x4096",
        "--workdir", str(root), "--dry-run"])
    assert result.exit_code == 0, result.output
    run = RunStore(root / "j").all()[0]
    prompt = run.prompt_path.read_text(encoding="utf-8")
    assert "整幅 360 全景重绘" in prompt
    assert "图1 = 待重绘的 360 底图" in prompt
    assert "图2 = 掩码图" in prompt
    assert "图3 = 氛围与光影参考图" in prompt
    assert "夜宴氛围" in prompt
    assert "两份拷贝" in prompt
    assert "白色区域" in prompt
    # 冗余带列号按 --canvas/--source 算出：带 683 px，主内容 8192 px。
    assert "画布宽 9558 像素" in prompt
    assert "最左侧 683 像素（第 0–682 列）" in prompt
    assert "最右侧 683 像素（第 8875–9557 列）" in prompt
    assert "中间 8192 像素（第 683–8874 列）" in prompt
    assert "{" not in prompt and "|" not in prompt


def test_image_submit_requires_canvas_and_source_together(tmp_path):
    images = _images(tmp_path)
    root = tmp_path / "runs"
    jobs.edit_prompt_vars(
        root, "j", sets={"edit": "x", "preserve": "y"},
        template="fullRedrawImg", vars_file="still-vars.toml")
    result = runner.invoke(vrgen_cli.app, [
        "image", "submit", "--job", "j",
        "--template", "fullRedrawImg", "--vars-file", "still-vars.toml",
        "--image", str(images[0]), "--image-role", "edit_master",
        "--canvas", "9558x4096",
        "--workdir", str(root), "--dry-run"])
    assert result.exit_code != 0
    assert "--canvas 与 --source 必须成对给出" in result.output


def test_still360_local_template_renders(tmp_path):
    images = _images(tmp_path)
    root = tmp_path / "runs"
    jobs.edit_prompt_vars(
        root, "j",
        sets={"edit": "把画中的人换成长袍文官",
              "preserve": "机位、场景与光照保持图1原样"},
        template="still360-local", vars_file="still-vars.toml")
    result = runner.invoke(vrgen_cli.app, [
        "image", "submit", "--job", "j",
        "--template", "still360-local", "--vars-file", "still-vars.toml",
        "--image-role", "edit_master", "--image-role", "mask",
        "--image", str(images[0]), "--image", str(images[1]),
        "--workdir", str(root), "--dry-run"])
    assert result.exit_code == 0, result.output
    run = RunStore(root / "j").all()[0]
    prompt = run.prompt_path.read_text(encoding="utf-8")
    assert "图1 = 待重绘的 360 底图" in prompt
    assert "图2 = 掩码图" in prompt
    assert "长袍文官" in prompt
    assert "{" not in prompt and "|" not in prompt


def test_ref_image_validator_finds_a_nested_block():
    data = {"modelConfig": {"referenceModes": [
        {"mode": "edit", "refImageValidator": {"maxWidth": 10}}]}}
    assert image_mod._ref_image_validator(data) == {"maxWidth": 10}
    assert image_mod._ref_image_validator({"modelConfig": {}}) == {}


def test_check_input_images_rejects_a_too_wide_input(tmp_path, monkeypatch):
    images = _images(tmp_path, 1)
    from vrgen.prep.budget import InputSpec
    monkeypatch.setattr(
        image_mod, "probe_image",
        lambda path, config=None: InputSpec(
            width=64, height=32, fps=0.0, duration_s=0.0, size_bytes=100,
            container=("png",)))
    with pytest.raises(UsageError, match="width"):
        image_mod._check_input_images(
            _conf(tmp_path), images,
            {"modelConfig": {"referenceModes": [
                {"refImageValidator": {"maxWidth": 10}}]}})
    # No validator published: the check is a no-op, not a false failure.
    image_mod._check_input_images(_conf(tmp_path), images, {"modelConfig": {}})


def test_image_submit_renders_detail_refs(tmp_path):
    images = _images(tmp_path, 3)
    root = tmp_path / "runs"
    _seed_keyframe_vars(root)
    result = runner.invoke(vrgen_cli.app, [
        "image", "submit", "--job", "j",
        "--template", "keyframe-first", "--vars-file", "keyframe-vars.toml",
        "--step", "first",
        "--image-role", "first_frame_raw", "--image-role", "subject_ref",
        "--image-role", "detail_ref",
        "--image-note", "", "--image-note", "", "--image-note", "头冠纹样与金色饰物",
        "--image", str(images[0]), "--image", str(images[1]), "--image", str(images[2]),
        "--workdir", str(root), "--dry-run"])
    assert result.exit_code == 0, result.output
    run = RunStore(root / "j").all()[0]
    prompt = run.prompt_path.read_text(encoding="utf-8")
    assert "图3 = 细节参考图" in prompt
    assert "头冠纹样与金色饰物" in prompt
    assert "图3 = 首帧定稿" not in prompt


def test_image_submit_rejects_note_count_mismatch(tmp_path):
    images = _images(tmp_path)
    root = tmp_path / "runs"
    _seed_keyframe_vars(root)
    result = runner.invoke(vrgen_cli.app, [
        "image", "submit", "--job", "j",
        "--template", "keyframe-first", "--vars-file", "keyframe-vars.toml",
        "--image-role", "first_frame_raw", "--image-role", "subject_ref",
        "--image-note", "只有一条",
        "--image", str(images[0]), "--image", str(images[1]),
        "--workdir", str(root), "--dry-run"])
    assert result.exit_code == 2


def test_image_submit_rejects_note_without_template(tmp_path):
    images = _images(tmp_path)
    result = runner.invoke(vrgen_cli.app, [
        "image", "submit", "--job", "j", "--prompt", "p",
        "--image-note", "x",
        "--image", str(images[0]),
        "--workdir", str(tmp_path / "runs"), "--dry-run"])
    assert result.exit_code == 2


def test_prompt_show_renders_image_roles_with_notes(tmp_path):
    root = tmp_path / "runs"
    _seed_keyframe_vars(root)
    result = runner.invoke(vrgen_cli.app, [
        "prompt", "show", "--job", "j",
        "--template", "keyframe-first", "--vars-file", "keyframe-vars.toml",
        "--image-role", "first_frame_raw", "--image-role", "subject_ref",
        "--image-role", "detail_ref",
        "--image-note", "", "--image-note", "", "--image-note", "袖口纹样",
        "--workdir", str(root)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert "图3 = 细节参考图" in payload["rendered"]
    assert "袖口纹样" in payload["rendered"]


def test_keyframe_templates_do_not_hardcode_positions():
    for name in ("keyframe-first.md", "keyframe-last.md"):
        text = (templates_dir() / name).read_text(encoding="utf-8")
        assert "图2" not in text
        assert "图3" not in text


def test_prompt_show_renders_image_roles(tmp_path):
    root = tmp_path / "runs"
    _seed_keyframe_vars(root)
    result = runner.invoke(vrgen_cli.app, [
        "prompt", "show", "--job", "j",
        "--template", "keyframe-first", "--vars-file", "keyframe-vars.toml",
        "--image-role", "first_frame_raw", "--image-role", "subject_ref",
        "--workdir", str(root)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert "image_roles" in payload["computed_keys"]
    assert "图1 = 首帧原始图" in payload["rendered"]
    assert "图2 = 人物三视图" in payload["rendered"]


def test_keyframe_vars_file_isolated(tmp_path):
    root = tmp_path / "runs"
    _seed_keyframe_vars(root)
    assert jobs.load_prompt_vars(root, "j").vars == {}
    assert jobs.load_prompt_vars(root, "j", "keyframe-vars.toml").vars["figure"]


def test_keyframe_templates_share_shared_aliases():
    first = (templates_dir() / "keyframe-first.md").read_text(encoding="utf-8")
    last = (templates_dir() / "keyframe-last.md").read_text(encoding="utf-8")
    for key in ("target", "preserve", "cleanup"):
        assert template_aliases(first)[key] == template_aliases(last)[key]
        assert template_help(first)[key] == template_help(last)[key]


def test_keyframe_templates_have_no_case_specific_residue():
    """旧案例的占位人形与界面清理不能再写死在正文里（2026-10-30 决策 50）。"""
    for name in ("keyframe-first.md", "keyframe-last.md"):
        text = (templates_dir() / name).read_text(encoding="utf-8")
        for phrase in ("白色占位人形", "不继承", "【界面清理】"):
            assert phrase not in text, (name, phrase)
        assert "{target|" in text
        assert "{cleanup|" in text


def test_keyframe_last_uses_its_own_subject_slot():
    """尾帧可能是空场，外观不能沿用首帧模板的 figure 槽位。"""
    text = (templates_dir() / "keyframe-last.md").read_text(encoding="utf-8")
    assert "{subject_last|" in text
    assert "{figure|" not in text


def test_result_index_copies_nth_candidate(tmp_path):
    images = _images(tmp_path)
    conf = _conf(tmp_path)
    pkg = _package(tmp_path, images)
    prepared = prepare_image_round(conf, pkg, log=Log(quiet=True))
    run = prepared.run
    results = []
    for index in range(1, 4):
        path = run.dir / f"result_{index}.png"
        path.write_bytes(f"candidate-{index}".encode())
        results.append(str(path))
    run.results = results
    run.gate = {"verdict": "ok", "kind": "image", "images": []}
    run.save()

    out = tmp_path / "picked.png"
    result = runner.invoke(vrgen_cli.app, [
        "result", "--job", "j", "--round", run.key[:12],
        "--index", "3", "--out", str(out), "--workdir", str(tmp_path / "runs")])
    assert result.exit_code == 0, result.output
    assert out.read_bytes() == b"candidate-3"


def test_status_lists_image_rounds(tmp_path):
    images = _images(tmp_path)
    conf = _conf(tmp_path)
    pkg = _package(tmp_path, images)
    prepared = prepare_image_round(conf, pkg, log=Log(quiet=True))
    run = prepared.run
    run.results = [str(run.dir / f"result_{i}.png") for i in range(1, 4)]
    for path in run.results:
        Path(path).write_bytes(b"png")
    run.gate = {"verdict": "ok", "kind": "image", "images": []}
    run.save()

    result = runner.invoke(vrgen_cli.app, [
        "status", "--job", "j", "--workdir", str(tmp_path / "runs")])
    assert result.exit_code == 0, result.output
    assert "[img]" in result.output and "result=x3" in result.output
