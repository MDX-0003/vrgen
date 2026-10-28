"""素材角色注册表：本仓库唯一的词表，内容来自同目录的 ``roles.json``。

``roles.json`` 是根仓库 ``docs/roles.json`` 的逐字节副本（契约快照）。
``tests/test_roles_contract.py`` 断言这里的派生常量与快照一致，并用 SHA256
钉住快照内容；改角色要三份文件一起改，见 ``docs/case-model.md`` §2。

vrgen 只关心提示词侧：角色名、写进素材清单的职责句、种类限制、保留角色。
标签、下拉可见性与模式归属由 vrstudio 与前端消费，同样来自这份快照。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

_SPEC_PATH = Path(__file__).with_name("roles.json")


@dataclass(frozen=True)
class RoleSpec:
    name: str
    label: str
    help: str
    prompt: str
    kinds: tuple[str, ...] | None
    modes: tuple[str, ...]
    open: bool
    reserved: bool


def _load() -> tuple[RoleSpec, ...]:
    data = json.loads(_SPEC_PATH.read_text(encoding="utf-8"))
    return tuple(
        RoleSpec(
            name=str(item["name"]),
            label=str(item["label"]),
            help=str(item["help"]),
            prompt=str(item["prompt"]),
            kinds=(tuple(str(kind) for kind in item["kinds"])
                   if item.get("kinds") else None),
            modes=tuple(str(mode) for mode in item.get("modes") or ()),
            open=bool(item["open"]),
            reserved=bool(item["reserved"]),
        )
        for item in data["roles"]
    )


ROLE_SPECS: tuple[RoleSpec, ...] = _load()
ROLES = tuple(spec.name for spec in ROLE_SPECS)
#: 提示词素材清单里的职责句。
ROLE_PROMPT = {spec.name: spec.prompt for spec in ROLE_SPECS}
#: 只对列出的素材种类成立的角色；未列入 = 不限。
ROLE_KINDS: dict[str, tuple[str, ...]] = {
    spec.name: spec.kinds for spec in ROLE_SPECS if spec.kinds
}
RESERVED_ROLES = tuple(spec.name for spec in ROLE_SPECS if spec.reserved)
#: 只给关键帧工作台当前端输入的角色；命名约定 ``*_raw``，不能进入 refs 清单。
RAW_ROLES = tuple(name for name in ROLES if name.endswith("_raw"))


def check_kinds(role: str, kind: str) -> str | None:
    """角色能否用于该素材种类；不能则返回人读原因。"""
    allowed = ROLE_KINDS.get(role)
    if allowed and kind not in allowed:
        return f"角色 {role} 只能用于 {' / '.join(allowed)} 素材，当前是 {kind}"
    return None
