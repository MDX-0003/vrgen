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
from .paths import templates_dir
from .prep.prompt import GEOMETRY_KEYS, template_placeholders
from .round.ledger import Run, RunStore

PROMPT_VARS = "prompt-vars.toml"

#: 模板占位里由素材与模式在 submit 时计算的只读键（决策 25）。它们不属于
#: ``--set`` 的草稿变量，也不允许写进 prompt-vars.toml。
COMPUTED_KEYS = ("asset_list", "task_rule", "timeline_rule")

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


# ── generation.toml：生成页的两个分辨率选择（决策 20）────────────────────

GENERATION = "generation.toml"


@dataclass
class GenerationParams:
    resolution: str | None = None   # mglc 输出档位 480p/720p/1080p
    scale: str | None = None        # vrgen 上传缩放 auto/full/half
    mode: str | None = None         # mglc 生成模式 videoEdit/allMediaRef/videoExtend
    aspect_ratio: str | None = None # 输出比例；auto = 跟随输入（无比例选项的模式）
    session_id: int | None = None   # >0 = 项目生成会话；计费走项目额度
    project_id: int | None = None   # 仅用于页面回显会话所属项目
    episode: int | None = None      # 仅用于页面回显会话所属剧集


def generation_params_path(workdir: str | Path | None, job: str) -> Path:
    return job_dir(workdir, job) / GENERATION


def load_generation_params(workdir: str | Path | None, job: str) -> GenerationParams:
    path = generation_params_path(workdir, job)
    if not path.is_file():
        return GenerationParams()
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    resolution = data.get("resolution")
    scale = data.get("scale")
    mode = data.get("mode")
    aspect_ratio = data.get("aspect_ratio")
    session_id = data.get("session_id")
    project_id = data.get("project_id")
    episode = data.get("episode")
    for key, value in (("resolution", resolution), ("scale", scale),
                       ("mode", mode), ("aspect_ratio", aspect_ratio)):
        if value is not None and not isinstance(value, str):
            raise UsageError(f"{path}: {key} 必须是字符串")
    for key, value in (("session_id", session_id), ("project_id", project_id),
                       ("episode", episode)):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise UsageError(f"{path}: {key} 必须是整数")
        if value is not None and value < 0:
            raise UsageError(f"{path}: {key} 必须 >= 0")
    return GenerationParams(resolution=resolution, scale=scale,
                            mode=mode, aspect_ratio=aspect_ratio,
                            session_id=session_id or None,
                            project_id=project_id or None,
                            episode=episode or None)


def save_generation_params(workdir: str | Path | None, job: str,
                           params: GenerationParams) -> Path:
    path = generation_params_path(workdir, job)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if params.resolution is not None:
        lines.append(_toml_str("resolution", params.resolution))
    if params.scale is not None:
        lines.append(_toml_str("scale", params.scale))
    if params.mode is not None:
        lines.append(_toml_str("mode", params.mode))
    if params.aspect_ratio is not None:
        lines.append(_toml_str("aspect_ratio", params.aspect_ratio))
    if params.session_id:
        lines.append(f"session_id = {int(params.session_id)}")
    if params.project_id:
        lines.append(f"project_id = {int(params.project_id)}")
    if params.episode:
        lines.append(f"episode = {int(params.episode)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ── 提示词模板 + 变量的读改视图（CLI prompt show/save 的本体）────────────


def _resolve_template(template: str | None, pv: PromptVars,
                      config_default: str | None) -> str | None:
    """生效模板：显式指定 > 草稿记录 > 配置默认。"""
    resolved = template or pv.template or (
        str(config_default).strip() if config_default else None)
    if resolved and not (templates_dir() / f"{resolved}.md").is_file():
        available = sorted(p.stem for p in templates_dir().glob("*.md"))
        raise UsageError(f"模板 {resolved!r} 不存在；可用：{available}")
    return resolved or None


def prompt_overview(workdir, job, template: str | None = None,
                    config_default: str | None = None) -> dict:
    """``prompt show`` 的本体：生效模板、正文、占位符、几何键、草稿 vars。

    返回结构是 CLI 契约（docs/case-model.md §4），JSON 输出给调用方（web）解析；
    还没选过模板时 template_text 为 null、其余照常，页面据此显示空态。
    """
    pv = load_prompt_vars(workdir, job)
    resolved = _resolve_template(template, pv, config_default)
    overview: dict = {
        "template": resolved,
        "available": sorted(p.stem for p in templates_dir().glob("*.md")),
        "template_text": None,
        "placeholders": [],
        "geometry_keys": [],
        "computed_keys": [],
        "vars": pv.vars,
    }
    if resolved:
        text = (templates_dir() / f"{resolved}.md").read_text(encoding="utf-8")
        placeholders = template_placeholders(text)
        overview.update(template_text=text,
                        placeholders=placeholders,
                        geometry_keys=[k for k in placeholders if k in GEOMETRY_KEYS],
                        computed_keys=[k for k in placeholders if k in COMPUTED_KEYS])
    return overview


def edit_prompt_vars(workdir, job, sets: dict[str, str] | None = None,
                     unsets: list[str] | None = None, template: str | None = None,
                     config_default: str | None = None) -> dict:
    """``prompt save`` 的本体：改草稿并写回，返回保存后的 :func:`prompt_overview`。

    与 submit ``--set`` 的差别：这里是草稿编辑器，允许出现新键；模板正文引用的
    键不许删（删了 submit 时模板展开必失败，这里提前 fail loud）。
    """
    pv = load_prompt_vars(workdir, job)
    resolved = _resolve_template(template, pv, config_default)
    computed = sorted(set(COMPUTED_KEYS) & set(sets or {}))
    if computed:
        raise UsageError(f"变量 {computed} 由素材与模式自动生成，不能写入草稿")
    if unsets and resolved:
        protected = set(template_placeholders(
            (templates_dir() / f"{resolved}.md").read_text(encoding="utf-8")))
        blocked = sorted(protected & set(unsets))
        if blocked:
            raise UsageError(f"变量 {blocked} 被模板正文引用，不能删除；只能修改它的值")
    merged = dict(pv.vars)
    merged.update(sets or {})
    for key in unsets or []:
        if key not in merged:
            known = ", ".join(sorted(merged)) or "(空)"
            raise UsageError(f"未知变量 {key!r}；本 job 可用：{known}")
        del merged[key]
    save_prompt_vars(workdir, job, PromptVars(template=resolved, vars=merged))
    return prompt_overview(workdir, job, config_default=config_default)


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
