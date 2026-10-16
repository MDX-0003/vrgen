"""job 管理：台账树的分组键 + prompt-vars 草稿。

job 只是一个字符串键（调用方通常传 case 名），vrgen 不知道也不关心它的含义。
这是从 vrpad ``aigc/case.py`` 里活下来的那一半灵魂："约定只说一次，每轮只传增量"；
"去 vrpad 的 case 目录里自己找 *_pad.json"的那一半被砍掉，输入一律显式入参。
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .errors import UsageError, VrgenError
from .ledger import RESULT_MP4, RUN_JSON, RunRecord

PROMPT_VARS = "prompt-vars.toml"

#: job 名只允许这组字符：它是台账树的目录名，不能让它逃出 workdir。
_JOB_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

DEFAULT_WORKDIR = "./vrgen_runs"


def resolve_workdir(workdir: str | Path | None = None) -> Path:
    """``--workdir`` > ``$VRGEN_RUNS_DIR`` > ``./vrgen_runs``。"""
    if workdir is not None:
        return Path(workdir)
    env = os.environ.get("VRGEN_RUNS_DIR")
    return Path(env) if env else Path(DEFAULT_WORKDIR)


def job_dir(workdir: str | Path | None, job: str) -> Path:
    if not _JOB_NAME.match(job):
        raise UsageError(
            f"job 名 {job!r} 不合法（只允许字母数字与 ._-，且不以符号开头）"
        )
    return resolve_workdir(workdir) / job


# ── prompt-vars 草稿 ─────────────────────────────────────────────────────


@dataclass
class PromptVars:
    """job 级草稿：模板选择 + 可变块。

    ``vars`` 的键集合就是 ``--set`` 的合法键集合（自适应：往 toml 里加字段即可用，
    实现里没有也不允许有逐变量的固定分支）。
    """

    template: str | None = None
    vars: dict[str, str] = field(default_factory=dict)


def prompt_vars_path(workdir: str | Path | None, job: str) -> Path:
    return job_dir(workdir, job) / PROMPT_VARS


def load_prompt_vars(workdir: str | Path | None, job: str) -> PromptVars:
    path = prompt_vars_path(workdir, job)
    if not path.is_file():
        return PromptVars()
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    variables = data.get("vars", {})
    if not isinstance(variables, dict):
        raise UsageError(f"{path}: [vars] 必须是表")
    return PromptVars(template=data.get("template"), vars=dict(variables))


def save_prompt_vars(workdir: str | Path | None, job: str, pv: PromptVars) -> Path:
    """写回草稿。值用 JSON 字符串转义——JSON 的字符串规则是 TOML 基本串的子集，
    中文/引号/反斜杠都能安全往返。"""
    path = prompt_vars_path(workdir, job)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if pv.template is not None:
        lines.append(_toml_str("template", pv.template))
    lines.append("")
    lines.append("[vars]")
    for key, value in pv.vars.items():
        lines.append(_toml_str(key, value))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _toml_str(key: str, value: str) -> str:
    return f"{key} = {json.dumps(value, ensure_ascii=False)}"


def apply_set(pv: PromptVars, overrides: dict[str, str]) -> PromptVars:
    """``--set`` 的语义：键必须已存在于 [vars]（fail loud，列出可用键）；
    返回本次合并结果，调用方决定是否写回草稿。"""
    unknown = [k for k in overrides if k not in pv.vars]
    if unknown:
        known = ", ".join(sorted(pv.vars)) or "(空——先在 prompt-vars.toml 里定义变量)"
        raise UsageError(f"未知变量 {unknown}；本 job 可用：{known}")
    merged = PromptVars(template=pv.template, vars=dict(pv.vars))
    merged.vars.update(overrides)
    return merged


# ── 轮次目录 ─────────────────────────────────────────────────────────────


def round_dir(workdir: str | Path | None, job: str, round_key: str) -> Path:
    return job_dir(workdir, job) / round_key


def new_round_dir(workdir: str | Path | None, job: str, round_key: str) -> Path:
    """创建轮次目录；已存在则原样返回（幂等——同内容的重复注册是同一轮）。"""
    d = round_dir(workdir, job, round_key)
    d.mkdir(parents=True, exist_ok=True)
    return d


def list_rounds(workdir: str | Path | None, job: str) -> list[RunRecord]:
    """按创建时间升序列出该 job 的全部轮次；坏台账跳过，不让它阻塞列表。"""
    root = job_dir(workdir, job)
    if not root.is_dir():
        return []
    records = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or not (child / RUN_JSON).is_file():
            continue
        try:
            records.append(RunRecord.load(child))
        except VrgenError:
            continue
    records.sort(key=lambda r: (r.created_utc, r.round))
    return records


def latest_usable_round(workdir: str | Path | None, job: str) -> RunRecord:
    """gate != error 的最新一轮（ok 与 skipped 都算可用——skipped 只是没能验证，
    不是验证失败）。没有可用轮时报错。"""
    all_rounds = list_rounds(workdir, job)
    usable = [r for r in all_rounds if r.gate.get("verdict") != "error"]
    if not usable:
        root = job_dir(workdir, job)
        raise VrgenError(
            f"job {job} 没有可用的轮次（台账根 {root}，"
            f"共 {len(all_rounds)} 轮且全部 gate=error）"
        )
    return usable[-1]


def find_round(workdir: str | Path | None, job: str, round_key: str) -> RunRecord:
    d = round_dir(workdir, job, round_key)
    if not (d / RUN_JSON).is_file():
        raise UsageError(
            f"job {job} 没有轮次 {round_key}（查可用轮次：vrgen status --job {job}）"
        )
    return RunRecord.load(d)


def result_path(record: RunRecord, workdir: str | Path | None) -> Path:
    p = round_dir(workdir, record.job, record.round) / RESULT_MP4
    if not p.is_file():
        raise VrgenError(f"{p} 缺失——台账在而结果文件不在，这轮废了")
    return p
