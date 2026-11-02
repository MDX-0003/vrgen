"""Render the handoff prompt: a fixed block plus this round's variable block.

The fixed block is the part that must be *byte-identical across rounds* -- it is
what keeps the model's behaviour stable while the theme changes -- and the numbers
in it (canvas size, band width, column ranges) must match the file being uploaded.
Hand-copying them is exactly the mistake this module exists to prevent: upload at
half scale and a hand-written "4668x2000, columns 0-667" now describes a file that
does not exist. So the numbers are rendered from an ``UploadGeometry``.

The variable block comes from a per-round "brief" file, so a round is: same
template, different brief.
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path

from ..errors import AigcError
from .scale import UploadGeometry

#: Placeholders offered to a template, on top of whatever the brief defines.
GEOMETRY_KEYS = (
    "canvas_w", "canvas_h", "upload_w", "upload_h",
    "band_w", "band_half", "band_pct", "band_last_left", "left_first", "left_last",
    "main_first", "main_last", "main_w", "right_first", "right_last",
    "source_w", "source_h", "scale_pct",
)

_PLACEHOLDER = re.compile(
    r"\{([A-Za-z_][A-Za-z0-9_]*)(?:\|([^{}\r\n|]*))?(?:\|([^{}\r\n]*))?\}")


def template_placeholders(template_text: str) -> list[str]:
    """模板正文引用的全部 ``{键}``，按首次出现顺序去重。

    占位符可带页面显示名与填写说明（``{subject|原主体|写画面里…}``）；
    键名只看第一个 ``|`` 前的部分。只认标识符形态的占位符；字面量花括号
    （``{{…}}``）按尽力而为处理——正文真有花括号时 render 在 format 阶段就会
    报 malformed，不会静默出错。
    """
    seen: dict[str, None] = {}
    for m in _PLACEHOLDER.finditer(template_text):
        seen.setdefault(m.group(1), None)
    return list(seen)


def template_aliases(template_text: str) -> dict[str, str]:
    """``{键|显示名}`` 里的显示名，键 -> 显示名；同名键以第一次为准。

    显示名只服务页面，不进入展开后的提示词。没有写显示名的键不出现在结果里，
    调用方退回键名显示。
    """
    out: dict[str, str] = {}
    for m in _PLACEHOLDER.finditer(template_text):
        label = (m.group(2) or "").strip()
        if label:
            out.setdefault(m.group(1), label)
    return out


def template_help(template_text: str) -> dict[str, str]:
    """``{键|显示名|填写说明}`` 里的说明，键 -> 说明；同名键以第一次为准。

    说明只服务页面，回答“为什么要填这个字段”；不进入展开后的提示词。
    """
    out: dict[str, str] = {}
    for m in _PLACEHOLDER.finditer(template_text):
        help_text = (m.group(3) or "").strip()
        if help_text:
            out.setdefault(m.group(1), help_text)
    return out


def strip_aliases(template_text: str) -> str:
    """把 ``{键|显示名|说明}`` 归一成 ``{键}``，供 ``format_map`` 展开。

    只替换合法占位符；其他花括号原样保留，仍由 format 阶段报 malformed。
    """
    return _PLACEHOLDER.sub(lambda m: "{" + m.group(1) + "}", template_text)



def geometry_variables(geom: UploadGeometry) -> dict[str, object]:
    """The placeholder values that describe the upload raster."""
    w, h = geom.canvas_size
    return {
        "canvas_w": w,
        "canvas_h": h,
        "upload_w": w,
        "upload_h": h,
        "source_w": geom.source_size[0],
        "source_h": geom.source_size[1],
        "band_half": geom.P,
        "scale_pct": f"{100 * geom.scale:.1f}",
        **column_variables(geom.canvas_size, geom.band_w),
    }


def column_variables(canvas_size: tuple[int, int],
                     band_w: int) -> dict[str, object]:
    """列区间占位符：冗余带宽度与三段列号，只依赖画布宽度与带宽。

    视频（pad 产物有精确的源宽）与静图（上传副本尺寸由装配器缩到平台限额内，
    以模型侧画布为准）共用这一套。
    """
    w, h = int(canvas_size[0]), int(canvas_size[1])
    band = max(0, min(int(band_w), w // 2))
    left, main, right = (0, band), (band, w - band), (w - band, w)
    return {
        "band_w": band,
        "band_pct": f"{100 * band / w:.1f}",
        "band_last_left": band - 1,
        "left_first": left[0],
        "left_last": left[1] - 1,
        "main_first": main[0],
        "main_last": main[1] - 1,
        "main_w": main[1] - main[0],
        "right_first": right[0],
        "right_last": right[1] - 1,
    }


def geometry_from_sizes(canvas_size: tuple[int, int],
                        source_size: tuple[int, int]) -> dict[str, object]:
    """按本地 canvas/source 构造预览用几何变量（上传缩放按 100% 处理）。

    带宽是 ``L = canvas_w - source_w``，与 mask 的白带同宽：``redundant_white``
    每侧标记 ``L`` 列（外侧副本 ``P`` 加相邻的 ``P`` 列真实内容）。早先这里取
    ``(w - sw) // 2``，等于 ``P``，提示词于是把白带说成一半宽、把主内容区说宽了
    ``P`` 列，与模型手上的 mask 不符。
    """
    w, h = canvas_size
    sw, sh = source_size
    band = max(0, w - sw)
    geom = UploadGeometry(
        canvas_size=(w, h),
        source_size=(sw, sh),
        band_w=band,
        region_left=(0, band),
        region_main=(band, w - band),
        region_right=(w - band, w),
        scale=1.0,
    )
    return geometry_variables(geom)


class _Variables(dict):
    """A dict that names the offender when a template asks for an unknown key."""

    def __missing__(self, key: str):
        known = ", ".join(sorted(self))
        raise AigcError(
            f"prompt template uses {{{key}}}, which nothing defines.\n"
            f"  available: {known}\n"
            f"  (geometry keys come from the manifest; anything else must be in the brief)"
        )


def load_brief(path: str | Path) -> dict[str, str]:
    """A per-round brief: TOML with string values, or plain text as ``{brief}``."""
    path = Path(path)
    if not path.is_file():
        raise AigcError(f"brief not found: {path}")
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".toml":
        try:
            raw = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise AigcError(f"brief {path}: {exc}") from exc
        out: dict[str, str] = {}
        for key, value in raw.items():
            if isinstance(value, dict):
                raise AigcError(f"brief {path}: [{key}] tables are not supported; "
                                f"use flat keys such as theme / style")
            out[key] = str(value)
        return out
    return {"brief": text.strip()}


#: 渲染后只剩一节标题、没有正文的行，例如 ``【清理】``。
_HEADING_ONLY = re.compile(r"^\s*【[^】]*】\s*$")


def _blank(value: object) -> bool:
    return value is None or not str(value).strip()


def render(template_text: str, variables: Mapping[str, object]) -> str:
    """Fill a template. Unknown placeholders are an error, not empty text.

    一行里引用的占位符全部为空、且渲染后只剩 ``【标题】`` 或空白时，整行删掉：
    空的可选段不该以裸标题的形式进入提示词。本行没有占位符的独立标题行不动，
    所以 ``gen-multi`` 那种「标题一行、内容下一行」的写法不会被误删。

    清理放在渲染这一步，``prompt show`` 的预览与 ``image/video submit`` 提交的
    正文因此是同一份文本；过滤 vars 做不到这件事——模板展开仍要求每个占位符
    有定义。
    """
    try:
        fmt = _Variables(variables)
        out: list[str] = []
        for line in strip_aliases(template_text).split("\n"):
            keys = template_placeholders(line)
            rendered = line.format_map(fmt)
            if keys and all(_blank(variables.get(key)) for key in keys) \
                    and (not rendered.strip() or _HEADING_ONLY.match(rendered)):
                continue
            out.append(rendered)
        return "\n".join(out)
    except AigcError:
        raise
    except (IndexError, ValueError) as exc:      # stray { } in the template
        raise AigcError(f"prompt template is malformed: {exc}") from exc


def resolve_template(template: str | Path, base: str | Path) -> Path:
    """Absolute template paths stay as they are; relative ones resolve from ``base``.

    ``base`` is the repo root, so the shipped default (``config/aigc-prompt-360.md``)
    is found no matter which directory the command was run from.
    """
    path = Path(template)
    return path if path.is_absolute() else Path(base) / path


def render_files(template: str | Path, geom: UploadGeometry,
                 brief: Mapping[str, str] | None = None) -> str:
    """Convenience: template file + geometry + brief -> prompt text."""
    template = Path(template)
    if not template.is_file():
        raise AigcError(f"prompt template not found: {template}")
    variables: dict[str, object] = dict(geometry_variables(geom))
    variables.update(brief or {})
    return render(template.read_text(encoding="utf-8"), variables)


def hash_prompt(text: str) -> str:
    """Stable short hash, for the ledger's spend-once key."""
    normalised = "\n".join(line.rstrip() for line in text.strip().splitlines())
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:16]
