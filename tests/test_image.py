"""Image round lifecycle and CLI surface (契约 case-model.md §4/§5)."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from vrgen import cli as vrgen_cli
from vrgen import config as config_mod
from vrgen.progress import Log
from vrgen.round import image as image_mod
from vrgen.round.image import ImagePackage, prepare_image_round
from vrgen.round.ledger import RunStore

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


def _images(tmp_path: Path) -> tuple[Path, ...]:
    out = []
    for index in (1, 2):
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
