"""jobs / ledger / manual 的单元测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vrgen import jobs, manual
from vrgen.errors import UsageError, VrgenError
from vrgen.ledger import RESULT_MP4, RUN_JSON, RunRecord, now_utc

# ── ledger 往返 ──────────────────────────────────────────────────────────


def _record(**kw) -> RunRecord:
    base = dict(
        job="t", round="manual-00000000", backend="manual", stage="done",
        created_utc=now_utc(),
        result={"path": RESULT_MP4, "source": "x.mp4", "bytes": 3, "sha256": "0" * 64},
        gate={"verdict": "ok", "width": 100, "height": 50},
    )
    base.update(kw)
    return RunRecord(**base)


def test_ledger_round_trip(tmp_path):
    rd = tmp_path / "t" / "manual-00000000"
    record = _record(params={"resolution": "1080p"}, notes="历史素材")
    path = record.save(rd)
    back = RunRecord.load(rd)
    assert back.job == record.job and back.round == record.round
    assert back.result == record.result and back.gate == record.gate
    assert back.params == {"resolution": "1080p"} and back.notes == "历史素材"
    raw = json.loads(path.read_text(encoding="utf-8"))
    for key in ("schema", "stage", "params", "prompt", "assets", "task_id",
                "result", "gate", "notes"):
        assert key in raw, f"契约字段缺失: {key}"       # docs/case-model.md §5


def test_ledger_rejects_wrong_schema(tmp_path):
    rd = tmp_path / "t" / "r"
    rd.mkdir(parents=True)
    (rd / RUN_JSON).write_text(json.dumps({"schema": 99}), encoding="utf-8")
    with pytest.raises(VrgenError):
        RunRecord.load(rd)


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
    # 草稿里加一个全新字段
    path = jobs.prompt_vars_path(tmp_path, "job1")
    path.write_text(path.read_text(encoding="utf-8") + 'mood = "安静"\n', encoding="utf-8")
    pv2 = jobs.load_prompt_vars(tmp_path, "job1")
    merged2 = jobs.apply_set(pv2, {"mood": "紧张"})
    assert merged2.vars["mood"] == "紧张"


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


def _make_round(workdir, job, key, gate, created):
    rd = jobs.new_round_dir(workdir, job, key)
    _record(job=job, round=key, gate={"verdict": gate}, created_utc=created).save(rd)
    (rd / RESULT_MP4).write_bytes(b"x")
    return rd


def test_latest_usable_skips_error_rounds(tmp_path):
    _make_round(tmp_path, "j", "manual-aaaa1111", "ok", "2026-01-01T00:00:00Z")
    _make_round(tmp_path, "j", "manual-bbbb2222", "error", "2026-01-02T00:00:00Z")
    _make_round(tmp_path, "j", "manual-cccc3333", "ok", "2026-01-03T00:00:00Z")
    _make_round(tmp_path, "j", "manual-dddd4444", "skipped", "2026-01-04T00:00:00Z")
    latest = jobs.latest_usable_round(tmp_path, "j")
    assert latest.round == "manual-dddd4444"   # skipped 也算可用，取最新


def test_latest_usable_with_no_rounds_raises(tmp_path):
    with pytest.raises(VrgenError):
        jobs.latest_usable_round(tmp_path, "empty")


# ── manual 注册 ──────────────────────────────────────────────────────────


def test_register_is_idempotent_and_content_addressed(tmp_path):
    f = tmp_path / "ai_out.mp4"
    f.write_bytes(b"fake video bytes")
    r1 = manual.register("job1", f, workdir=tmp_path)
    r2 = manual.register("job1", f, workdir=tmp_path)
    assert r1.round == r2.round == "manual-" + r1.result["sha256"][:8]
    assert (tmp_path / "job1" / r1.round / RESULT_MP4).read_bytes() == b"fake video bytes"
    # 草稿目录不因注册而出现（register 不碰提示词）
    assert not jobs.prompt_vars_path(tmp_path, "job1").exists()


def test_register_gate_on_real_video_is_ok_or_skipped(tmp_path, util_video):
    """真视频在有 ffprobe 的机器上 gate=ok；没有 ffprobe 则 skipped——两者都放行。
    假字节文件在两种环境下都最多 skipped（ffprobe 跑不起来），不会 error。"""
    r = manual.register("job1", util_video, workdir=tmp_path)
    assert r.gate["verdict"] in ("ok", "skipped")
    assert r.stage == "done"
