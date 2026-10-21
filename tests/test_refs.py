"""gen 多素材清单与角色矩阵（契约 case-model.md §4）。"""

from __future__ import annotations

import json

import pytest

from vrgen.errors import UsageError
from vrgen.refs import (
    RefAsset,
    asset_list,
    load_refs,
    master,
    mode_variables,
    validate_counts,
    validate_mode,
)


def _file(path, data=b"bytes"):
    path.write_bytes(data)
    return path


def _write_refs(tmp_path, assets):
    payload = {"assets": assets}
    path = tmp_path / "refs.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _refs(tmp_path):
    v1 = _file(tmp_path / "v1.mp4")
    v2 = _file(tmp_path / "v2.mp4")
    i1 = _file(tmp_path / "i1.png")
    path = _write_refs(tmp_path, [
        {"id": "v1", "kind": "video", "path": "v1.mp4", "role": "edit_master"},
        {"id": "v2", "kind": "video", "path": "v2.mp4", "role": "motion_ref"},
        {"id": "i1", "kind": "image", "path": "i1.png", "role": "subject_ref"},
    ])
    return path, v1, v2, i1


def test_load_refs_resolves_relative_paths_and_hashes(tmp_path):
    path, v1, _v2, _i1 = _refs(tmp_path)
    refs = load_refs(path)
    assert [a.id for a in refs] == ["v1", "v2", "i1"]
    assert refs[0].path == v1.resolve()
    assert refs[0].key == "video:v1"


def test_load_refs_rejects_duplicates_and_bad_roles(tmp_path):
    v = _file(tmp_path / "v.mp4")
    dup = _write_refs(tmp_path, [
        {"id": "v1", "kind": "video", "path": str(v), "role": "edit_master"},
        {"id": "v1", "kind": "video", "path": str(v), "role": "motion_ref"},
    ])
    with pytest.raises(UsageError, match="重复"):
        load_refs(dup)
    bad = _write_refs(tmp_path, [
        {"id": "v1", "kind": "video", "path": str(v), "role": "nope"},
    ])
    with pytest.raises(UsageError, match="role"):
        load_refs(bad)


def test_load_refs_checks_sha256(tmp_path):
    v = _file(tmp_path / "v.mp4", b"one")
    path = _write_refs(tmp_path, [
        {"id": "v1", "kind": "video", "path": str(v), "role": "edit_master",
         "sha256": "0" * 64},
    ])
    with pytest.raises(UsageError, match="sha256"):
        load_refs(path)


def test_semantic_rules_replace_the_role_matrix(tmp_path):
    path, _v1, _v2, _i1 = _refs(tmp_path)
    refs = load_refs(path)
    # 角色矩阵已退役：videoEdit 不再限制第二个视频的角色。
    validate_mode(refs, "videoEdit")
    with pytest.raises(UsageError, match="第一条视频"):
        validate_mode((refs[1], refs[0], refs[2]), "allMediaRef")
    extra_master = (refs[0], RefAsset("v3", "video", "edit_master", refs[1].path))
    with pytest.raises(UsageError, match="至多"):
        validate_mode(extra_master, "videoEdit")


def test_raw_roles_and_mask_rules(tmp_path):
    image = _file(tmp_path / "i.png")
    video = _file(tmp_path / "v.mp4")
    with pytest.raises(UsageError, match="工作台"):
        validate_mode((RefAsset("i1", "image", "first_frame_raw", image),), "allMediaRef")
    with pytest.raises(UsageError, match="mask"):
        validate_mode((RefAsset("mask", "video", "mask", video),), "videoEdit")


def test_mask_is_not_numbered(tmp_path):
    path, _v1, _v2, _i1 = _refs(tmp_path)
    refs = load_refs(path)
    mask = RefAsset("mask", "image", "mask", refs[2].path)
    text = asset_list(refs + (mask,))
    assert "@Image1：主体参考" in text
    assert "@Image2" not in text
    assert "编辑区域标注" not in text


def test_allmediaref_master_must_be_first(tmp_path):
    path, _v1, _v2, _i1 = _refs(tmp_path)
    refs = load_refs(path)
    swapped = (refs[1], refs[0], refs[2])
    with pytest.raises(UsageError, match="第一条视频"):
        validate_mode(swapped, "allMediaRef")


def test_counts_and_prompt_block(tmp_path):
    path, _v1, _v2, _i1 = _refs(tmp_path)
    refs = load_refs(path)
    validate_counts(refs, "allMediaRef", max_videos=2, max_images=1)
    with pytest.raises(UsageError, match="最多 1 个视频"):
        validate_counts(refs, "allMediaRef", max_videos=1, max_images=1)
    text = asset_list(refs)
    assert "@Video1：编辑母版" in text
    assert "@Video2：动作参考" in text
    assert "@Image1：主体参考" in text
    interleaved = asset_list((refs[0], refs[2], refs[1]))
    assert interleaved.index("@Video2") < interleaved.index("@Image1")
    assert master(refs).id == "v1"
    assert mode_variables("videoEdit")["timeline_rule"].startswith("动作顺序")
    all_media = mode_variables("allMediaRef")
    assert "锁定参考素材的空间" in all_media["task_rule"]
    assert "不必逐秒" in all_media["timeline_rule"]
    assert "迈步" in all_media["timeline_rule"]


def test_any_role_is_allowed_without_the_matrix(tmp_path):
    v = _file(tmp_path / "v.mp4")
    i = _file(tmp_path / "i.png")
    path = _write_refs(tmp_path, [
        {"id": "v1", "kind": "video", "path": str(v), "role": "space_ref"},
        {"id": "i1", "kind": "image", "path": str(i), "role": "space_ref"},
    ])
    refs = load_refs(path)
    validate_mode(refs, "allMediaRef")
    validate_mode(refs, "videoEdit")      # 语义规则里没有按模式的角色白名单
    assert "@Video1：空间参考" in asset_list(refs)
    assert "@Image1：空间参考" in asset_list(refs)


def test_allmediaref_accepts_image_only(tmp_path):
    i1 = _file(tmp_path / "i1.png")
    i2 = _file(tmp_path / "i2.png")
    path = _write_refs(tmp_path, [
        {"id": "i1", "kind": "image", "path": str(i1), "role": "subject_ref"},
        {"id": "i2", "kind": "image", "path": str(i2), "role": "scene_ref"},
    ])
    refs = load_refs(path)
    validate_mode(refs, "allMediaRef")
    assert master(refs).id == "i2"       # space/scene 图片在第一条图片之前


def test_first_last_frame_role_matrix(tmp_path):
    i1 = _file(tmp_path / "i1.png")
    i2 = _file(tmp_path / "i2.png")
    good = _write_refs(tmp_path, [
        {"id": "i1", "kind": "image", "path": str(i1), "role": "first_frame"},
        {"id": "i2", "kind": "image", "path": str(i2), "role": "last_frame"},
    ])
    refs = load_refs(good)
    validate_mode(refs, "firstLastFrame")
    with pytest.raises(UsageError, match="firstLastFrame.*顺序"):
        validate_mode((refs[1], refs[0]), "firstLastFrame")
    video = _file(tmp_path / "v.mp4")
    with pytest.raises(UsageError, match="不能出现视频"):
        validate_mode(refs + (RefAsset("v1", "video", "edit_master", video),),
                      "firstLastFrame")
    mid = _file(tmp_path / "i3.png")
    with pytest.raises(UsageError, match="恰好需要"):
        validate_mode(refs + (RefAsset("i3", "image", "mid_frame", mid),),
                      "firstLastFrame")


def test_master_fallback_chain(tmp_path):
    v1 = _file(tmp_path / "v1.mp4")
    v2 = _file(tmp_path / "v2.mp4")
    i1 = _file(tmp_path / "i1.png")
    i2 = _file(tmp_path / "i2.png")
    i3 = _file(tmp_path / "i3.png")
    assert master((RefAsset("v1", "video", "motion_ref", v1),)).id == "v1"
    assert master((RefAsset("v1", "video", "motion_ref", v1),
                   RefAsset("v2", "video", "edit_master", v2))).id == "v2"
    assert master((RefAsset("i1", "image", "subject_ref", i1),
                   RefAsset("i2", "image", "scene_ref", i2))).id == "i2"
    assert master((RefAsset("i1", "image", "subject_ref", i1),
                   RefAsset("i2", "image", "first_frame", i2))).id == "i2"
    assert master((RefAsset("i1", "image", "subject_ref", i1),
                   RefAsset("i3", "image", "last_frame", i3))).id == "i1"


def test_new_role_prompt_wording(tmp_path):
    i1 = _file(tmp_path / "i1.png")
    i2 = _file(tmp_path / "i2.png")
    i3 = _file(tmp_path / "i3.png")
    text = asset_list((
        RefAsset("i1", "image", "first_frame", i1),
        RefAsset("i2", "image", "last_frame", i2),
        RefAsset("i3", "image", "first_frame_raw", i3),
    ))
    assert "@Image1：首帧定稿图" in text
    assert "@Image2：尾帧定稿图" in text
    assert "@Image3：工作台输入底图" in text
    first_last = mode_variables("firstLastFrame")
    assert "首帧" in first_last["task_rule"]
    assert "尾帧" in first_last["task_rule"]
