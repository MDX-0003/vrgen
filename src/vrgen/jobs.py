"""job 管理：台账树的分组键 + prompt-vars 草稿。

job 只是一个字符串键（调用方通常传 case 名），vrgen 不知道也不关心它的含义。
轮次台账本体在 ``round/ledger.py``（← vrpad，战斗格式）；本模块只做 job 维度的
分组（``runs/<job>/<轮次>/``）与提示词草稿。
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .errors import UsageError, VrgenError
from .round.ledger import Run, RunStore

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


# ── 轮次（台账本体在 round/ledger.py，这里做 job 分组视图）───────────────


def store_for(workdir: str | Path | None, job: str) -> RunStore:
    return RunStore(job_dir(workdir, job))


def list_rounds(workdir: str | Path | None, job: str) -> list[Run]:
    """该 job 的全部轮次，按更新时间升序。"""
    rounds = store_for(workdir, job).all()
    rounds.sort(key=lambda r: (r.updated_utc or r.created_utc or "", r.key))
    return rounds


def latest_usable_round(workdir: str | Path | None, job: str) -> Run:
    """有结果文件且 gate != error 的最新一轮。"""
    usable = [r for r in list_rounds(workdir, job)
              if r.result and Path(r.result).is_file()
              and (r.gate or {}).get("verdict") != "error"]
    if not usable:
        raise VrgenError(
            f"job {job} 没有可用的轮次（台账根 {job_dir(workdir, job)}）"
        )
    return usable[-1]


def find_round(workdir: str | Path | None, job: str, round_key: str) -> Run:
    try:
        return store_for(workdir, job).open(round_key)
    except VrgenError as exc:
        raise UsageError(f"job {job}：{exc}") from exc


def result_path(run: Run) -> Path:
    p = run.result_path
    if p is None:
        # 从未生成过结果（提交失败/中断，只留下了过程记录）。
        # 字段名（run.result）级别的细节降级到这行注释，终端只留人话与下一步。
        raise VrgenError(
            f"Session {run.dir.name} 没有生成过结果视频（当时提交失败或中断，"
            f"只留下了过程记录），无法被后续流程使用；请换一个 Session，或重新提交生成")
    if not p.is_file():
        raise VrgenError(
            f"Session {run.dir.name} 记录的结果视频不在了：{p}"
            f"（可能被手动移动或删除）；请换一个 Session，或重新提交生成")
    return p
