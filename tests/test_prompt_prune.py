"""空的可选段在渲染阶段整行删除（决策 51）。"""

from __future__ import annotations

import pytest

from vrgen.errors import AigcError
from vrgen.prep.prompt import render


def test_empty_slot_drops_its_heading_line():
    text = "开头\n【清理】{cleanup|清理项（可空）|说明}\n【画质】写实\n"
    assert render(text, {"cleanup": ""}) == "开头\n【画质】写实\n"


def test_non_empty_slot_keeps_the_line():
    text = "【清理】{cleanup|清理项（可空）|说明}\n"
    assert render(text, {"cleanup": "去掉顶部工具栏"}) == "【清理】去掉顶部工具栏\n"


def test_heading_without_placeholder_is_kept():
    """gen-multi 的独立标题行不能因为下一行是占位符而被连带删除。"""
    text = "【素材与分工】\n{asset_list}\n【固定约束】\n1. 镜头不动\n"
    out = render(text, {"asset_list": "图1 = 主图"})
    assert "【素材与分工】" in out
    assert "【固定约束】" in out
    assert "图1 = 主图" in out


def test_line_with_literal_text_is_kept_even_when_placeholder_empty():
    """只有整行剩标题才删；行内还有别的文字就保留。"""
    text = "【必须保持】{preserve}（其余不变）\n"
    assert render(text, {"preserve": ""}) == "【必须保持】（其余不变）\n"


def test_placeholder_only_line_disappears_when_empty():
    text = "前\n{subject|主体|说明}\n后\n"
    assert render(text, {"subject": "  "}) == "前\n后\n"


def test_unknown_key_still_fails_loudly():
    with pytest.raises(AigcError, match="nothing defines"):
        render("【清理】{cleanup}\n", {})
