"""手动注册后端：把本地文件登记为一轮结果，写与 mglc 后端同构的台账。

替代 vrpatch 的 ``ai_clip.json``（一个裸路径指针，没有来历时record）。
注册后结果文件拷进轮次目录（自包含：原文件之后可删可移）。
台账本体用 ``round/ledger.py`` 的 Run（与 mglc 轮次同一格式）。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from . import jobs
from .errors import UsageError, VrgenError
from .round.ledger import Run, RunStore

RESULT_MP4 = "result.mp4"


def register(job: str, file: str | Path, workdir: str | Path | None = None,
             notes: str = "") -> Run:
    """登记一个本地文件为 ``job`` 的一轮结果。

    轮次 key = ``manual-<源文件sha256前8位>``——内容寻址，同一文件重复注册是
    同一轮（幂等），不同文件天然不同轮。
    """
    src = Path(file)
    if not src.is_file():
        raise UsageError(f"要注册的文件不存在：{src}")
    digest = _sha256(src)
    key = f"manual-{digest[:8]}"
    store = RunStore(jobs.job_dir(workdir, job))

    existing = store.find_by_key(key)
    if existing is not None and existing.result and Path(existing.result).is_file():
        return existing                       # 幂等：同文件同轮

    run = store.create(key, label="manual", params={
        "backend": "manual",
        "source": str(src.resolve()),
        "sha256": digest,
        "notes": notes,
    }, prompt="")
    result_file = run.dir / RESULT_MP4
    if not result_file.is_file():
        shutil.copyfile(src, result_file)

    gate = probe_gate(result_file)
    run.result = str(result_file)
    run.gate = gate
    if notes:
        run.note(notes)
    if gate.get("verdict") == "error":
        run.advance("failed")
        run.save()
        raise VrgenError(
            f"注册失败：{gate.get('reason', '闸门未通过')}（台账已留现场：{run.dir}）")
    run.advance("fetched")
    run.advance("checked")
    run.save()
    return run


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _ffprobe() -> str | None:
    """``$VRGEN_FFPROBE`` > vrgen 的二进制发现。没有就返回 None（闸门记 skipped）。"""
    override = os.environ.get("VRGEN_FFPROBE")
    if override and Path(override).is_file():
        return override
    from .media import ffprobe
    try:
        return ffprobe()
    except VrgenError:
        return None


def probe_gate(file: Path) -> dict:
    """通用闸门（决策 D3）：能解码出视频流 → ok（附分辨率/帧数/时长）；
    ffprobe 不可用或跑不起来 → skipped（没能验证）；
    ffprobe 明确说没有视频流 → error。几何闸门（带相关配准）归 vrstudio。"""
    probe = _ffprobe()
    if probe is None:
        return {"verdict": "skipped", "reason": "ffprobe 不可用（$VRGEN_FFPROBE / PATH）"}
    try:
        r = subprocess.run(
            [probe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,nb_frames",
             "-show_entries", "format=duration",
             "-of", "json", str(file)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"verdict": "skipped", "reason": f"ffprobe 没能运行：{exc}"}
    if r.returncode != 0:
        return {"verdict": "skipped",
                "reason": f"ffprobe 退出码 {r.returncode}：{(r.stderr or '').strip()[:200]}"}
    try:
        data = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return {"verdict": "skipped", "reason": "ffprobe 输出不是 JSON"}
    streams = data.get("streams") or []
    if not streams:
        return {"verdict": "error", "reason": "没有视频流（ffprobe 正常运行但报告 0 条视频流）"}
    s = streams[0]
    gate: dict = {"verdict": "ok", "width": s.get("width"), "height": s.get("height")}
    if s.get("nb_frames"):
        try:
            gate["frames"] = int(s["nb_frames"])
        except (TypeError, ValueError):
            pass
    try:
        gate["duration_s"] = round(float(data.get("format", {}).get("duration", 0)), 3)
    except (TypeError, ValueError):
        pass
    return gate
