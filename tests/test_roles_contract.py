"""roles.json 契约快照：本地词表必须与它一致，快照内容由 SHA256 钉住。

``roles.json`` 是根仓库 ``docs/roles.json`` 的逐字节副本。改角色时三份文件
一起改，并同步更新本文件的 ``CONTRACT_SHA256``，否则这里会红。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from vrgen import refs, roles

SPEC = Path(roles.__file__).with_name("roles.json")
CONTRACT_SHA256 = "4c7a53dde440e76a9208ea898e7bfb442dab092f8b4265c2df15b4b8bc451a1f"


def _entries() -> list[dict]:
    return json.loads(SPEC.read_text(encoding="utf-8"))["roles"]


def test_snapshot_matches_the_pinned_digest():
    assert hashlib.sha256(SPEC.read_bytes()).hexdigest() == CONTRACT_SHA256


def test_vocabulary_matches_the_snapshot():
    entries = _entries()
    assert list(roles.ROLES) == [item["name"] for item in entries]
    assert roles.ROLE_PROMPT == {item["name"]: item["prompt"] for item in entries}
    assert roles.ROLE_KINDS == {
        item["name"]: tuple(item["kinds"]) for item in entries if item["kinds"]
    }
    assert roles.RESERVED_ROLES == tuple(
        item["name"] for item in entries if item["reserved"])
    assert roles.RAW_ROLES == (
        "first_frame_raw", "last_frame_raw")


def test_refs_module_reexports_the_registry():
    assert refs.ROLES == roles.ROLES
    assert refs.ROLE_PROMPT == roles.ROLE_PROMPT
    assert refs.RAW_ROLES == roles.RAW_ROLES
