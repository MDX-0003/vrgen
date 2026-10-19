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


def test_role_matrix(tmp_path):
    path, _v1, _v2, _i1 = _refs(tmp_path)
    refs = load_refs(path)
    validate_mode(refs, "allMediaRef")
    with pytest.raises(UsageError, match="恰好需要 1 个视频"):
        validate_mode(refs, "videoEdit")
    with pytest.raises(UsageError, match="subject_ref"):
        validate_mode(tuple(a for a in refs if a.id != "v2")
                      + (RefAsset("v1b", "video", "subject_ref", refs[0].path),),
                      "allMediaRef")


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
    assert "重新生成完整表演" in all_media["task_rule"]
    assert "不必逐秒" in all_media["timeline_rule"]
    assert "迈步" in all_media["timeline_rule"]
