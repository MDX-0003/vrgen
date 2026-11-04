"""jobs / manual 的单元测试（台账本体迁至 round/ledger.py 后的适配版）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from vrgen import jobs, manual
from vrgen.errors import UsageError, VrgenError
from vrgen.round.ledger import RUN_FILE as RUN_JSON
from vrgen.round.ledger import Run, RunStore

# ── ledger 往返（契约字段：docs/case-model.md §5）────────────────────────


def _run(key: str, tmp: Path, **kw) -> Run:
    rd = tmp / key
    rd.mkdir(parents=True, exist_ok=True)
    run = Run(key=key, dir=rd, **kw)
    run.save()
    return run


def test_run_round_trip_and_contract_fields(tmp_path):
    run = _run("abc123", tmp_path, stage="checked",
               params={"backend": "mglc", "model_id": 37},
               prompt="【任务】…", gate={"verdict": "ok", "width": 2334},
               result=str(tmp_path / "abc123" / "result.mp4"),
               updated_utc="2026-10-16T00:00:00Z")
    back = Run.load(tmp_path / "abc123")
    assert back.key == run.key and back.params == run.params
    assert back.gate == run.gate and back.result == run.result
    import json
    raw = json.loads((tmp_path / "abc123" / RUN_JSON).read_text(encoding="utf-8"))
    for field_ in ("key", "stage", "params", "prompt", "task_id",
                   "result", "gate", "notes"):
        assert field_ in raw, f"契约字段缺失: {field_}"
    # assets 已退役（决策 75）：新轮次不再写该字段，读取方按缺省处理。
    assert "assets" not in raw


# ── prompt-vars：--set 自适应的核心 ──────────────────────────────────────


def test_prompt_vars_round_trip_chinese(tmp_path):
    pv = jobs.PromptVars(template="360-closure",
                         vars={"theme": "黄昏海面，鲸鱼", "style": '电影感 "自然光"'})
    jobs.save_prompt_vars(tmp_path, "job1", pv)
    back = jobs.load_prompt_vars(tmp_path, "job1")
    assert back.template == "360-closure"
    assert back.vars == {"theme": "黄昏海面，鲸鱼", "style": '电影感 "自然光"'}


def test_apply_set_adapts_to_new_fields(tmp_path):
    """新增 [vars] 字段后 --set 立即可用——不需要任何代码分支。"""
    jobs.save_prompt_vars(tmp_path, "job1", jobs.PromptVars(
        vars={"theme": "黄昏", "style": "电影感"}))
    pv = jobs.load_prompt_vars(tmp_path, "job1")
    merged = jobs.apply_set(pv, {"theme": "夜晚"})
    assert merged.vars["theme"] == "夜晚" and merged.vars["style"] == "电影感"
    path = jobs.prompt_vars_path(tmp_path, "job1")
    path.write_text(path.read_text(encoding="utf-8") + 'mood = "安静"\n', encoding="utf-8")
    pv2 = jobs.load_prompt_vars(tmp_path, "job1")
    assert jobs.apply_set(pv2, {"mood": "紧张"}).vars["mood"] == "紧张"


def test_apply_set_unknown_key_fails_loud(tmp_path):
    jobs.save_prompt_vars(tmp_path, "job1", jobs.PromptVars(vars={"theme": "黄昏"}))
    pv = jobs.load_prompt_vars(tmp_path, "job1")
    with pytest.raises(UsageError) as ei:
        jobs.apply_set(pv, {"themee": "夜晚"})
    assert "theme" in str(ei.value)          # 报错必须列出可用键


def test_job_name_rejects_path_escape():
    with pytest.raises(UsageError):
        jobs.job_dir(None, "../escape")
    with pytest.raises(UsageError):
        jobs.job_dir(None, "a/b")


def test_workdir_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("VRGEN_RUNS_DIR", str(tmp_path / "envdir"))
    assert jobs.resolve_workdir(None) == tmp_path / "envdir"
    assert jobs.resolve_workdir(tmp_path / "flagdir") == tmp_path / "flagdir"
    monkeypatch.delenv("VRGEN_RUNS_DIR")
    assert jobs.resolve_workdir(None) == Path(jobs.DEFAULT_WORKDIR)


# ── 轮次列举 / latest 语义 ───────────────────────────────────────────────


def _make_round(workdir, job, key, gate, updated, result=True):
    rd = jobs.job_dir(workdir, job) / key
    rd.mkdir(parents=True, exist_ok=True)
    run = Run(key=key, dir=rd, stage="checked", gate={"verdict": gate},
              updated_utc=updated)
    if result:
        (rd / "result.mp4").write_bytes(b"x")
        run.result = str(rd / "result.mp4")
    run.save()
    return run


def test_latest_usable_skips_error_rounds(tmp_path):
    _make_round(tmp_path, "j", "manual-aaaa1111", "ok", "2026-01-01T00:00:00Z")
    _make_round(tmp_path, "j", "manual-bbbb2222", "error", "2026-01-02T00:00:00Z")
    _make_round(tmp_path, "j", "manual-cccc3333", "ok", "2026-01-03T00:00:00Z")
    _make_round(tmp_path, "j", "manual-dddd4444", "skipped", "2026-01-04T00:00:00Z")
    assert jobs.latest_usable_round(tmp_path, "j").key == "manual-dddd4444"


def test_latest_usable_requires_a_result_file(tmp_path):
    _make_round(tmp_path, "j", "manual-eeee5555", "ok", "2026-01-05T00:00:00Z", result=False)
    with pytest.raises(VrgenError):
        jobs.latest_usable_round(tmp_path, "j")


def test_find_round_by_key_prefix(tmp_path):
    _make_round(tmp_path, "j", "manual-ffff6666", "ok", "2026-01-06T00:00:00Z")
    assert jobs.find_round(tmp_path, "j", "manual-ffff").key == "manual-ffff6666"


# ── manual 注册 ──────────────────────────────────────────────────────────


def test_register_is_idempotent_and_content_addressed(tmp_path):
    f = tmp_path / "ai_out.mp4"
    f.write_bytes(b"fake video bytes")
    r1 = manual.register("job1", f, workdir=tmp_path)
    r2 = manual.register("job1", f, workdir=tmp_path)
    assert r1.key == r2.key == "manual-" + r1.params["sha256"][:8]
    assert (tmp_path / "job1" / r1.dir.name / "result.mp4").read_bytes() == b"fake video bytes"
    assert (tmp_path / "job1" / r1.dir.name / RUN_JSON).is_file()
    assert r1.params["backend"] == "manual"
    assert not jobs.prompt_vars_path(tmp_path, "job1").exists()   # register 不碰提示词


def test_register_gate_on_real_video_is_ok_or_skipped(tmp_path, util_video):
    r = manual.register("job1", util_video, workdir=tmp_path)
    assert r.gate["verdict"] in ("ok", "skipped")
    assert r.stage == "checked"


def test_runstore_still_refuses_duplicate_keys(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"v")
    store = RunStore(tmp_path / "runs")
    store.create("k" * 20, params={}, prompt="p", upload=video)
    with pytest.raises(VrgenError):
        store.create("k" * 20, params={}, prompt="p")
