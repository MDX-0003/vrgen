"""提交清单：解析 ``--refs`` JSON、五条语义校验、生成提示词素材块。

素材清单是 vrstudio 与 vrgen 之间的唯一提交入口（契约
``docs/case-model.md`` §4）。vrgen 不知道 case.toml 的存在，只认这份清单里的
``id`` / ``kind`` / ``path`` / ``role`` / ``sha256``。三个流程（patch/fullredraw/
gen）共用同一种清单：流程产物 clip/pad 与文件素材没有区别，mask 是装配器自动
附带的内部控制图。

数量、格式、时长、比例的上限以模型详情的 validator 为唯一依据；这里只保留平台
接不住的五条语义规则：firstLastFrame 恰好一对首尾帧、至多一个 edit_master 且
必须是第一条视频、mask 只能是图片、lighting_ref 只能是图片、``*_raw`` 角色不能
进入 refs。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from .errors import UsageError

KINDS = ("video", "image")
ROLES = (
    "edit_master", "motion_ref", "space_ref", "scene_ref", "lighting_ref",
    "subject_ref",
    "first_frame", "last_frame", "mid_frame", "first_frame_raw", "last_frame_raw",
    "mask",
)

#: 只给关键帧工作台当前端输入的角色；不能进入 refs 清单。
RAW_ROLES = ("first_frame_raw", "last_frame_raw")

SUPPORTED_MODES = ("videoEdit", "allMediaRef", "videoExtend", "firstLastFrame")

#: 清单 id：字母开头，其余字母数字下划线连字符（用户素材 v1/i1、流程产物 clip/pad…）。
_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")

#: 提示词里的角色说明；顺序与 case.toml 的 assets 顺序一致。
ROLE_PROMPT = {
    "edit_master": "编辑母版，负责时间轴、构图与动作基准。",
    "motion_ref": "动作参考，只提供动作节奏与路径。",
    "space_ref": "空间参考，只提供机位、构图、起点与终点位置；不提供人物动作。",
    "scene_ref": "场景参考，只提供构图、背景与光照。",
    "lighting_ref": "光照参考，只提供光源位置、色温、明暗对比与空气质感；"
                    "不提供构图、机位、背景与任何画面内容。",
    "subject_ref": "主体参考，只定义角色外观。",
    "first_frame": "首帧定稿图，视频的第一帧，锁定起点构图与人物外观。",
    "last_frame": "尾帧定稿图，视频的最后一帧，锁定终点位置、大小与朝向。",
    "mid_frame": "中间过渡帧，仅在首尾间距过大时补充。",
    "first_frame_raw": "工作台输入底图（原始抽取帧），只提供场景与占位，不是生成素材。",
    "last_frame_raw": "工作台输入底图（原始抽取帧），只提供场景与占位，不是生成素材。",
    "mask": "编辑区域标注，不作为画面内容参考。",
}

_ROLE_RULES = {
    "videoEdit": {
        "task": "只编辑编辑母版 @Video1：把指定主体重绘成目标外观；"
                "其他视频只作动作或场景参考，图片只定义角色外观或提供光照参考。",
        "timeline": "动作顺序、起止时间、行进方向与画面位置与编辑母版一致；"
                    "不得插入新的动作事件。",
    },
    "allMediaRef": {
        "task": "锁定参考素材的空间、镜头与场景，只替换指定主体并按【动作段】重演其动作；"
                "不重新设计站位、构图或镜头。",
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
    "firstLastFrame": {
        "task": "以首帧为起点、尾帧为终点生成中间表演；镜头与两端的构图锁定。",
        "timeline": "用连续的动作顺序描述首尾之间的过程：起始 → 起身 → 迈步 → 站定；"
                    "重点写清起步脚、抬脚与落脚、膝盖弯曲、重心转移、"
                    "身体上下起伏、肩胯转动与摆臂，让人看出是在迈步而不是整体平移。",
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
        if not _ID.match(asset_id):
            raise UsageError(
                f"{path}: 素材 id {asset_id!r} 不合约"
                f"（字母开头，其余字母数字下划线连字符，如 v1 / i1 / clip / mask）")
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
    """四条语义规则；数量与格式由模型详情 validator 负责。"""
    if mode not in SUPPORTED_MODES:
        raise UsageError(
            f"模式 {mode!r} 不在本期支持范围（{', '.join(SUPPORTED_MODES)}）")
    for asset in refs:
        if asset.role in RAW_ROLES:
            raise UsageError(
                f"素材 {asset.id} 的角色 {asset.role} 只用于关键帧工作台，不能提交")
        if asset.role == "mask" and asset.kind != "image":
            raise UsageError(f"素材 {asset.id} 的 mask 角色只能是图片")
        if asset.role == "lighting_ref" and asset.kind != "image":
            raise UsageError(
                f"素材 {asset.id} 的 lighting_ref 角色只能是图片（光照参考是单帧图片）")
    videos = [a for a in refs if a.kind == "video"]
    masters = [a for a in videos if a.role == "edit_master"]
    if len(masters) > 1:
        raise UsageError("至多允许 1 个视频使用 edit_master")
    if masters and videos[0] is not masters[0]:
        raise UsageError("edit_master 必须是第一条视频")
    if mode == "firstLastFrame":
        first = [a for a in refs if a.role == "first_frame"]
        last = [a for a in refs if a.role == "last_frame"]
        if videos or len(refs) != 2 or len(first) != 1 or len(last) != 1:
            raise UsageError(
                "模式 firstLastFrame 恰好需要 1 个 first_frame 和 1 个 last_frame，"
                "且不能出现视频、mask 或其他素材")
        if refs[0].role != "first_frame" or refs[1].role != "last_frame":
            raise UsageError(
                "模式 firstLastFrame 的素材顺序必须是 first_frame 在前、last_frame 在后")


def validate_counts(refs: tuple[RefAsset, ...], mode: str, *,
                    max_videos: int, max_images: int) -> None:
    videos = sum(1 for a in refs if a.kind == "video")
    images = sum(1 for a in refs if a.kind == "image")
    if max_videos and videos > max_videos:
        raise UsageError(f"模式 {mode} 最多 {max_videos} 个视频素材，当前 {videos} 个")
    if max_images and images > max_images:
        raise UsageError(f"模式 {mode} 最多 {max_images} 个图片素材，当前 {images} 个")


def master(refs: tuple[RefAsset, ...]) -> RefAsset:
    """几何与上传基准：按契约的回退链取第一条可用素材（mask 不算候选）。"""
    videos = [a for a in refs if a.kind == "video"]
    for asset in videos:
        if asset.role == "edit_master":
            return asset
    if videos:
        return videos[0]
    images = [a for a in refs if a.kind == "image" and a.role != "mask"]
    for asset in images:
        if asset.role in ("space_ref", "scene_ref"):
            return asset
    for asset in images:
        if asset.role == "first_frame":
            return asset
    if images:
        return images[0]
    raise UsageError("素材清单里没有可用作母版的素材")


def duration_source(mode: str) -> str:
    if mode == "firstLastFrame":
        return "none"
    return "longest" if mode == "allMediaRef" else "master"


def asset_list(refs: tuple[RefAsset, ...]) -> str:
    """``@Video1：…`` / ``@Image1：…``。

    视频与图片各自按清单顺序编号；清单块里视频在前、图片在后，避免一条图片
    把视频列表截成两段（mglc 的 ``--video`` / ``--image`` 本来就是两组）。
    mask 是编辑区域控制图，不参与编号（模板用固定文案说明最后一张图）。
    """
    counters = {"video": 0, "image": 0}
    lines: list[str] = []
    ordered = ([a for a in refs if a.kind == "video"]
               + [a for a in refs if a.kind == "image" and a.role != "mask"])
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
