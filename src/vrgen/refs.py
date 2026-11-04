"""提交清单：解析 ``--refs`` JSON、五条语义校验、生成提示词素材块。

素材清单是 vrstudio 与 vrgen 之间的唯一提交入口（契约
``docs/case-model.md`` §4）。vrgen 不知道 case.toml 的存在，只认这份清单里的
``id`` / ``kind`` / ``path`` / ``role`` / ``sha256``。三个流程（patch/fullredraw/
gen）共用同一种清单：流程产物 clip/pad 与文件素材没有区别，mask 是装配器自动
附带的内部控制图。

数量、格式、时长、比例的上限以模型详情的 validator 为唯一依据；这里只保留平台
接不住的语义规则：firstLastFrame 恰好一对首尾帧、至多一个 edit_master 且必须是
第一条视频、``*_raw`` 角色不能进入 refs，以及 ``roles.json`` 里的种类限制
（``check_kinds``，例如 mask 与 lighting_ref 只能是图片）。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from .errors import UsageError
from .roles import RAW_ROLES, ROLE_PROMPT, ROLES, check_kinds

KINDS = ("video", "image")

SUPPORTED_MODES = ("videoEdit", "allMediaRef", "videoExtend", "firstLastFrame")

#: 清单 id：字母开头，其余字母数字下划线连字符（用户素材 v1/i1、流程产物 clip/pad…）。
_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")

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
    """语义规则；数量与格式由模型详情 validator 负责。"""
    if mode not in SUPPORTED_MODES:
        raise UsageError(
            f"模式 {mode!r} 不在本期支持范围（{', '.join(SUPPORTED_MODES)}）")
    for asset in refs:
        if asset.role in RAW_ROLES:
            raise UsageError(
                f"素材 {asset.id} 的角色 {asset.role} 只用于关键帧工作台，不能提交")
        reason = check_kinds(asset.role, asset.kind)
        if reason:
            raise UsageError(f"素材 {asset.id}：{reason}")
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


MASK_NOTE = ("最后一张图片：区域标注图（黑白）。白色是左右两条冗余带，"
             "黑色是主内容区；它只说明区域位置，不是画面内容。")
MASK_BAN = "不出现最后一张图片的黑白色块与边界线；"
"""模板里的区域标注段；清单没有 mask 时由 :func:`mask_variables` 置空。"""


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


def mask_variables(refs: tuple[RefAsset, ...]) -> dict[str, str]:
    """模板的区域标注占位：有 mask 时给固定文案，没有时为空串。

    空串让 ``prompt.render`` 把只有该占位符的行整行删掉；``mask_ban`` 在
    ``【禁止】`` 行中间，空串不会删行，只会去掉那一句。
    """
    present = any(a.kind == "image" and a.role == "mask" for a in refs)
    return {"mask_note": MASK_NOTE if present else "",
            "mask_ban": MASK_BAN if present else ""}


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
