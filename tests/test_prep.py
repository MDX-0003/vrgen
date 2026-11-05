"""上传件准备：规划、离线产出、与 submit 的字节一致（决策 87）。"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

import vrgen.round.pipeline as pipe
from vrgen import config as config_mod
from vrgen.cli import app
from vrgen.prep import uploads as uploads_mod
from vrgen.prep.budget import InputSpec
from vrgen.progress import Log
from vrgen.refs import RefAsset
from vrgen.round.pipeline import InputPackage

runner = CliRunner()

CANVAS = (4668, 2000)          # 9,336,000 px：超过 8,295,044
SOURCE = (4000, 2000)
MAX_PIXELS = 8_295_044

TEMPLATE = ("canvas {canvas_w}x{canvas_h}; band {band_w}; "
            "left {left_first}-{left_last}; right {right_first}-{right_last}; "
            "theme {theme}")


def _model_cache(workdir: Path, *, max_pixel: int = MAX_PIXELS,
                 min_pixel: int = 409_600) -> None:
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "models.json").write_text(json.dumps({
        "fetched_at": time.time(),
        "items": [{"code": 37, "displayName": "Seedance 2.5",
                   "modelConfig": {"maxPromptLength": 10000, "referenceModes": [
                       {"mode": "videoEdit", "supportVirtualIp": True, "maxCount": 10,
                        "resolutions": ["480p", "720p", "1080p"],
                        "duration": {"min": 4, "max": 30},
                        "refVideoValidator": {
                            "minCount": 1, "maxCount": 10, "formats": ["mp4", "mov"],
                            "fps": {"min": 24, "max": 60},
                            "duration": {"min": 2, "max": 30},
                            "refVideoMinAspectRatio": 0.4,
                            "refVideoMaxAspectRatio": 2.5,
                            "maxBytes": 209_715_200,
                            "minPixel": min_pixel,
                            "maxPixel": max_pixel}}]}}],
    }), encoding="utf-8")


def _offline(monkeypatch, *, size: tuple[int, int] = CANVAS) -> None:
    """probe/scale 全离线：源按 ``size``，缩放件按文件名里的 WxH。"""

    def fake_probe_input(path, conf):
        name = Path(path).name
        if name.startswith("upload_"):
            width, height = (int(p) for p in
                             name[len("upload_"):].split(".")[0].rsplit("_", 1)[1]
                             .split("x"))
            return InputSpec(width=width, height=height, fps=30.0, duration_s=8.0,
                             size_bytes=7_700_000, container=("mp4",))
        return InputSpec(width=size[0], height=size[1], fps=30.0, duration_s=8.0,
                         size_bytes=228_200_000, container=("mp4",))

    def fake_probe_image(path, conf):
        name = Path(path).name
        if name.startswith("upload_"):
            width, height = (int(p) for p in
                             name[len("upload_"):].split(".")[0].rsplit("_", 1)[1]
                             .split("x"))
        else:
            width, height = size
        return InputSpec(width=width, height=height, fps=0.0, duration_s=0.0,
                         size_bytes=100_000 if name.startswith("upload_") else 8_600_000,
                         container=("png",))

    def fake_scale(src, dst, dst_size, **kw):
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_bytes(b"scaled")
        return Path(dst)

    monkeypatch.setattr(uploads_mod, "probe_input", fake_probe_input)
    monkeypatch.setattr(uploads_mod, "probe_image", fake_probe_image)
    monkeypatch.setattr(uploads_mod, "scale_video", fake_scale)
    monkeypatch.setattr(uploads_mod, "scale_image", fake_scale)
    monkeypatch.setattr(uploads_mod, "scale_reference_image", fake_scale)


def _files(tmp_path: Path) -> tuple[Path, Path, Path]:
    video = tmp_path / "pad.mp4"
    video.write_bytes(b"video-bytes")
    mask = tmp_path / "pad_mask.png"
    mask.write_bytes(b"mask-bytes")
    return video, mask, tmp_path / "derived"


def test_plan_video_passes_through_when_it_fits(monkeypatch):
    monkeypatch.setattr(uploads_mod, "probe_input", lambda path, conf: InputSpec(
        width=1920, height=1080, fps=30.0, duration_s=5.0,
        size_bytes=1_000_000, container=("mp4",)))
    plan = uploads_mod.plan_video(
        "pad", Path("x.mp4"), conf=None, mode_name="auto",
        validator={"formats": ["mp4"], "fps": {"min": 24, "max": 60},
                   "duration": {"min": 2, "max": 30},
                   "minPixel": 409_600, "maxPixel": MAX_PIXELS,
                   "maxBytes": 209_715_200,
                   "refVideoMinAspectRatio": 0.4,
                   "refVideoMaxAspectRatio": 2.5})
    assert plan.size == (1920, 1080)
    assert plan.needs_scale is False


def test_prep_writes_scaled_upload_mask_and_sidecar(tmp_path, monkeypatch):
    _offline(monkeypatch)
    video, mask, out = _files(tmp_path)
    runs = tmp_path / "runs"
    _model_cache(runs / "j")
    r = runner.invoke(app, [
        "prep", "--video", str(video), "--mask", str(mask),
        "--canvas", "4668x2000", "--source", "4000x2000",
        "--out", str(out), "--job", "j", "--workdir", str(runs)])
    assert r.exit_code == 0, r.output
    sidecars = list(out.glob("upload_pad_*.json"))
    assert len(sidecars) == 1
    blob = json.loads(sidecars[0].read_text(encoding="utf-8"))
    upload = blob["upload"]
    assert blob["scaled"] is True
    assert upload["pixels"] <= MAX_PIXELS
    assert upload["width"] % 2 == 0 and upload["height"] % 2 == 0
    assert blob["canvas"] == [4668, 2000] and blob["source"] == [4000, 2000]
    assert blob["limit"]["max_pixel"] == MAX_PIXELS
    assert blob["band"]["L"] == 2 * blob["band"]["P"] > 0
    regions = blob["band"]["regions"]
    assert regions["B"][1] - regions["B"][0] == blob["band"]["L"]
    assert regions["R"][1] - regions["R"][0] == upload["width"] - 2 * blob["band"]["L"]
    stem = f"upload_pad_{upload['width']}x{upload['height']}"
    assert (out / f"{stem}.mp4").is_file()
    mask_stem = f"upload_pad_mask_{upload['width']}x{upload['height']}"
    assert (out / f"{mask_stem}.png").is_file()
    assert blob["mask"]["width"] == upload["width"]


def test_prep_dry_run_writes_nothing(tmp_path, monkeypatch):
    _offline(monkeypatch)
    video, mask, out = _files(tmp_path)
    runs = tmp_path / "runs"
    _model_cache(runs / "j")
    r = runner.invoke(app, [
        "prep", "--video", str(video), "--mask", str(mask),
        "--canvas", "4668x2000", "--source", "4000x2000",
        "--out", str(out), "--job", "j", "--workdir", str(runs), "--dry-run"])
    assert r.exit_code == 0, r.output
    assert "dry-run" in r.output
    assert not out.exists()


def test_prep_full_scale_over_limit_is_a_budget_error(tmp_path, monkeypatch):
    _offline(monkeypatch)
    video, mask, out = _files(tmp_path)
    runs = tmp_path / "runs"
    _model_cache(runs / "j")
    r = runner.invoke(app, [
        "prep", "--video", str(video), "--mask", str(mask),
        "--canvas", "4668x2000", "--source", "4000x2000",
        "--out", str(out), "--job", "j", "--workdir", str(runs), "--scale", "full"])
    # 与 submit 一致：预算是运行失败（1），不是参数错（2）。
    assert r.exit_code == 1
    assert "仍不合约" in r.output
    assert not out.exists()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="需要真实 ffmpeg")
def test_prep_and_submit_produce_the_same_upload_bytes(tmp_path, monkeypatch):
    """同一输入、同一参数下，导出件与轮次目录里的 upload_pad_*.mp4 必须逐字节相同。"""
    video = tmp_path / "pad.mp4"
    subprocess.run(
        [shutil.which("ffmpeg"), "-y", "-v", "error", "-f", "lavfi", "-i",
         "color=c=blue:s=800x600:d=3", "-r", "30", "-pix_fmt", "yuv420p", str(video)],
        check=True, capture_output=True)
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "t.md").write_text(TEMPLATE, encoding="utf-8")
    monkeypatch.setattr(pipe, "templates_dir", lambda: templates)
    import vrgen.cli as vrgen_cli
    monkeypatch.setattr(vrgen_cli, "templates_dir", lambda: templates)

    runs = tmp_path / "runs"
    _model_cache(runs / "j", max_pixel=4_000, min_pixel=100)
    conf = config_mod.load(overrides=["mglc.scale=auto"])
    pkg = InputPackage(job="j", workdir=runs / "j", template="t",
                       variables={"theme": "黄昏"},
                       refs=(RefAsset("pad", "video", "edit_master", video),),
                       canvas_size=(800, 600), source_size=(800, 600))
    prepared = pipe.prepare_round(conf, pkg, log=Log(quiet=True))
    submit_upload = Path(prepared.run.upload["path"])
    assert submit_upload.name.startswith("upload_pad_")

    out = tmp_path / "derived"
    r = runner.invoke(app, [
        "prep", "--video", str(video), "--canvas", "800x600", "--source", "800x600",
        "--out", str(out), "--job", "j", "--workdir", str(runs)])
    assert r.exit_code == 0, r.output
    exported = next(out.glob("upload_pad_*.mp4"))
    assert exported.name == submit_upload.name
    assert (hashlib.sha256(exported.read_bytes()).hexdigest()
            == hashlib.sha256(submit_upload.read_bytes()).hexdigest())
