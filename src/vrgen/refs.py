"""gen 多素材清单：解析 ``--refs`` JSON、校验角色矩阵、生成提示词素材块。

素材清单是 vrstudio 与 vrgen 之间唯一的多素材入口（契约
``docs/case-model.md`` §4）。vrgen 不知道 case.toml 的存在，只认这份清单里的
``id`` / ``kind`` / ``path`` / ``role`` / ``sha256``。

角色矩阵与 ``vrstudio.case.MODE_ROLES`` 是同一份规则的镜像；两仓库不能互相
import（AGENTS.md 硬规则 1），改动时必须同时改两侧与 ``docs/case-model.md``。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .errors import UsageError

KINDS = ("video", "image")
ROLES = ("edit_master", "motion_ref", "scene_ref", "subject_ref")

#: 本期支持的模式与允许的角色；与 vrstudio.case.MODE_ROLES 保持一致。
MODE_ROLES: dict[str, dict[str, tuple[str, ...]]] = {
    "videoEdit": {"video": ("edit_master",), "image": ("subject_ref",)},
    "allMediaRef": {"video": ("edit_master", "motion_ref", "scene_ref"),
                    "image": ("subject_ref", "scene_ref")},
    "videoExtend": {"video": ("edit_master",), "image": ("subject_ref",)},
}
SUPPORTED_MODES = tuple(MODE_ROLES)

#: 提示词里的角色说明；顺序与 case.toml 的 assets 顺序一致。
ROLE_PROMPT = {
    "edit_master": "编辑母版，负责时间轴、构图与动作基准。",
    "motion_ref": "动作参考，只提供动作节奏与路径。",
    "scene_ref": "场景参考，只提供构图、背景与光照。",
    "subject_ref": "主体参考，只定义角色外观。",
}

_ROLE_RULES = {
    "videoEdit": {
        "task": "只编辑编辑母版 @Video1：把指定主体重绘成目标外观；"
                "其他视频只作动作或场景参考，图片只定义角色外观。",
        "timeline": "动作顺序、起止时间、行进方向与画面位置与编辑母版一致；"
                    "不得插入新的动作事件。",
    },
    "allMediaRef": {
        "task": "按下面的情节与动作顺序重新生成完整表演；"
                "素材只提供身份、动作或场景参考，不要求逐帧复刻任何一条视频。",
        "timeline": "用连续的动作顺序描述：起始 → 起身 → 迈步 → 站定；"
                    "不必逐秒拆分。重点写清起步脚、抬脚与落脚、膝盖弯曲、"
                    "重心转移、身体上下起伏、肩胯转动与摆臂，"
                    "让人看出是在迈步而不是整体平移。",
    },
    "videoExtend": {
        "task": "从编辑母版 @Video1 的结尾继续生成，保持人物、镜头、场景与光照连续。",
        "timeline": "不重演母版已有的时段；新内容从母版最后一帧之后开始，"
                    "动作与节奏自然接续。",
    },
}


@dataclass(frozen=True)
class RefAsset:
    id: str
    kind: str
    role: str
    path: Path
    sha256: str | None = None

    @property
    def key(self) -> str:
        """台账 assets 的分组键，如 ``video:v1``。"""
        return f"{self.kind}:{self.id}"


def load_refs(path: str | Path) -> tuple[RefAsset, ...]:
    """读并校验素材清单；路径相对 JSON 文件所在目录解析。"""
    path = Path(path)
    if not path.is_file():
        raise UsageError(f"素材清单不存在：{path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UsageError(f"{path}: 不是合法 JSON：{exc}") from exc
    items = data.get("assets") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        raise UsageError(f"{path}: 需要非空的 assets 数组")
    base = path.parent
    out: list[RefAsset] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise UsageError(f"{path}: assets[{index}] 必须是对象")
        asset_id = str(item.get("id") or "")
        kind = str(item.get("kind") or "")
        role = str(item.get("role") or "")
        raw = str(item.get("path") or "")
        sha = item.get("sha256")
        if not asset_id:
            raise UsageError(f"{path}: assets[{index}] 缺 id")
        if asset_id in seen:
            raise UsageError(f"{path}: 素材 id 重复：{asset_id}")
        seen.add(asset_id)
        if kind not in KINDS:
            raise UsageError(f"{path}: 素材 {asset_id} 的 kind 必须是 {' 或 '.join(KINDS)}")
        prefix = "v" if kind == "video" else "i"
        if not asset_id.startswith(prefix) or not asset_id[1:].isdigit():
            raise UsageError(
                f"{path}: 素材 id {asset_id!r} 不合约（{kind} 用 {prefix}1、{prefix}2…）")
        if role not in ROLES:
            raise UsageError(f"{path}: 素材 {asset_id} 的 role {role!r} 不合约")
        if not raw:
            raise UsageError(f"{path}: 素材 {asset_id} 缺 path")
        file = Path(raw)
        if not file.is_absolute():
            file = (base / file).resolve()
        if not file.is_file():
            raise UsageError(f"{path}: 素材 {asset_id} 的文件不存在：{file}")
        if sha is not None:
            actual = _sha256(file)
            if str(sha) != actual:
                raise UsageError(
                    f"{path}: 素材 {asset_id} 的 sha256 与文件不一致：{file}")
        out.append(RefAsset(id=asset_id, kind=kind, role=role, path=file,
                            sha256=str(sha) if sha is not None else None))
    return tuple(out)


def validate_mode(refs: tuple[RefAsset, ...], mode: str) -> None:
    """角色矩阵校验：模式、种类、角色、数量与顺序。"""
    if mode not in MODE_ROLES:
        raise UsageError(
            f"模式 {mode!r} 不在本期支持范围（{', '.join(SUPPORTED_MODES)}）")
    allowed = MODE_ROLES[mode]
    videos = [a for a in refs if a.kind == "video"]
    if not videos:
        raise UsageError(f"模式 {mode} 至少需要 1 个视频素材")
    if mode in ("videoEdit", "videoExtend") and len(videos) != 1:
        raise UsageError(f"模式 {mode} 恰好需要 1 个视频素材，当前 {len(videos)} 个")
    for asset in refs:
        if asset.role not in allowed[asset.kind]:
            raise UsageError(
                f"模式 {mode} 不允许 {asset.kind} 使用角色 {asset.role}；"
                f"可选：{', '.join(allowed[asset.kind])}")
    if mode in ("videoEdit", "videoExtend"):
        if videos[0].role != "edit_master":
            raise UsageError(f"模式 {mode} 的视频角色必须是 edit_master")
        return
    masters = [a for a in videos if a.role == "edit_master"]
    if len(masters) > 1:
        raise UsageError("allMediaRef 至多允许 1 个视频使用 edit_master")
    if masters and videos[0] is not masters[0]:
        raise UsageError("allMediaRef 的 edit_master 必须是第一条视频")


def validate_counts(refs: tuple[RefAsset, ...], mode: str, *,
                    max_videos: int, max_images: int) -> None:
    videos = sum(1 for a in refs if a.kind == "video")
    images = sum(1 for a in refs if a.kind == "image")
    if max_videos and videos > max_videos:
        raise UsageError(f"模式 {mode} 最多 {max_videos} 个视频素材，当前 {videos} 个")
    if max_images and images > max_images:
        raise UsageError(f"模式 {mode} 最多 {max_images} 个图片素材，当前 {images} 个")


def master(refs: tuple[RefAsset, ...]) -> RefAsset:
    """几何与上传基准：第一条 edit_master，没有则第一条视频。"""
    videos = [a for a in refs if a.kind == "video"]
    if not videos:
        raise UsageError("素材清单里没有视频")
    for asset in videos:
        if asset.role == "edit_master":
            return asset
    return videos[0]


def duration_source(mode: str) -> str:
    return "longest" if mode == "allMediaRef" else "master"


def asset_list(refs: tuple[RefAsset, ...]) -> str:
    """``@Video1：…`` / ``@Image1：…``。

    视频与图片各自按清单顺序编号；清单块里视频在前、图片在后，避免一条图片
    把视频列表截成两段（mglc 的 ``--video`` / ``--image`` 本来就是两组）。
    """
    counters = {"video": 0, "image": 0}
    lines: list[str] = []
    ordered = ([a for a in refs if a.kind == "video"]
               + [a for a in refs if a.kind == "image"])
    for asset in ordered:
        counters[asset.kind] += 1
        label = ("Video" if asset.kind == "video" else "Image") + str(counters[asset.kind])
        lines.append(f"@{label}：{ROLE_PROMPT[asset.role]}")
    return "\n".join(lines)


def mode_variables(mode: str) -> dict[str, str]:
    if mode not in _ROLE_RULES:
        raise UsageError(f"模式 {mode!r} 不在本期支持范围（{', '.join(SUPPORTED_MODES)}）")
    rules = _ROLE_RULES[mode]
    return {"task_rule": rules["task"], "timeline_rule": rules["timeline"]}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
