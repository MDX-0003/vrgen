"""模板别名：``{键|显示名}`` 的解析、渲染与出厂模板覆盖。"""

from __future__ import annotations

import re

import pytest

from vrgen import jobs
from vrgen.paths import templates_dir
from vrgen.prep.prompt import (
    GEOMETRY_KEYS,
    geometry_from_sizes,
    render,
    strip_aliases,
    template_aliases,
    template_help,
    template_placeholders,
)

_ALIASED = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)(?:\|([^{}\r\n|]*))?(?:\|([^{}\r\n]*))?\}")


def test_placeholders_ignore_alias_and_aliases_collect_it():
    text = ("把 {subject|原主体|写要被替换的主体} 重绘成 "
            "{target|重绘结果|写替换后的外观}；{plot} 保持。")
    assert template_placeholders(text) == ["subject", "target", "plot"]
    assert template_aliases(text) == {"subject": "原主体", "target": "重绘结果"}
    assert template_help(text) == {
        "subject": "写要被替换的主体", "target": "写替换后的外观"}


def test_render_strips_alias_and_keeps_text_identical():
    aliased = ("情节：{plot|情节|写动作}\n"
               "任务：{subject|原主体|写主体} -> {target|重绘结果|写结果}")
    plain = strip_aliases(aliased)
    assert "{plot}" in plain and "|" not in plain
    variables = {"plot": "跳一支舞", "subject": "白色人偶", "target": "真人"}
    out = render(aliased, variables)
    assert out == "情节：跳一支舞\n任务：白色人偶 -> 真人"
    assert out == render(plain, variables)
    assert "|" not in out and "{" not in out


def test_empty_alias_is_treated_as_absent():
    assert template_aliases("{subject|}") == {}
    assert template_help("{subject||写说明}") == {"subject": "写说明"}
    assert render("{subject|}", {"subject": "x"}) == "x"


def test_alias_with_spaces_and_slashes_is_kept():
    assert template_aliases("{theme|主题 / 场景}") == {"theme": "主题 / 场景"}


@pytest.mark.parametrize("path", sorted(templates_dir().glob("*.md")),
                         ids=lambda p: p.stem)
def test_shipped_templates_annotate_editable_placeholders(path):
    text = path.read_text(encoding="utf-8")
    aliases = template_aliases(text)
    helps = template_help(text)
    editable = set(template_placeholders(text)) - set(GEOMETRY_KEYS) - set(jobs.COMPUTED_KEYS)
    assert not sorted(editable - set(aliases)), f"{path.name} 缺显示名"
    assert not sorted(editable - set(helps)), f"{path.name} 缺填写说明"


@pytest.mark.parametrize("path", sorted(templates_dir().glob("*.md")),
                         ids=lambda p: p.stem)
def test_alias_is_consistent_within_one_template(path):
    seen: dict[str, str] = {}
    seen_help: dict[str, str] = {}
    for m in _ALIASED.finditer(path.read_text(encoding="utf-8")):
        key, label = m.group(1), (m.group(2) or "").strip()
        help_text = (m.group(3) or "").strip()
        for text in (label, help_text):
            assert "\n" not in text and "{" not in text and "}" not in text
        if label:
            if key in seen:
                assert seen[key] == label, f"{path.name} 的 {key} 出现了两个显示名"
            seen[key] = label
        if help_text:
            if key in seen_help:
                assert seen_help[key] == help_text, f"{path.name} 的 {key} 出现了两段说明"
            seen_help[key] = help_text


def test_prompt_overview_exposes_labels_and_help(tmp_path):
    payload = jobs.prompt_overview(tmp_path, "j1", template="character-repaint")
    assert payload["labels"]["subject"] == "原主体"
    assert payload["labels"]["plot"] == "情节"
    assert payload["help"]["subject"].startswith("写画面里")
    assert "asset_list" not in payload["labels"]      # 计算键不强制写别名


def test_geometry_band_matches_the_mask_band():
    """提示词的冗余带宽度必须等于 mask 的白带宽度（L = canvas_w - source_w）。

    ``redundant_white`` 每侧标白 ``L`` 列：外侧副本 ``P`` 加相邻的 ``P`` 列真实
    内容。早先这里按 ``P`` 渲染，提示词把白带说成一半宽、把主内容区说宽了 ``P`` 列，
    模型手上的 mask 与文字对不上。
    """
    variables = geometry_from_sizes((9558, 4096), (8192, 4096))
    assert variables["canvas_w"] == 9558
    assert variables["band_w"] == 1366          # L，不是 P=683
    assert variables["band_half"] == 683        # P
    assert (variables["left_first"], variables["left_last"]) == (0, 1365)
    assert (variables["main_first"], variables["main_last"]) == (1366, 8191)
    assert variables["main_w"] == 6826
    assert (variables["right_first"], variables["right_last"]) == (8192, 9557)


def test_geometry_band_is_zero_without_redundancy():
    """gen 一类的 canvas 与 source 同宽：带宽 0，主内容区就是整幅。"""
    variables = geometry_from_sizes((1920, 1080), (1920, 1080))
    assert variables["band_w"] == 0
    assert (variables["main_first"], variables["main_last"]) == (0, 1919)
    assert variables["main_w"] == 1920
