"""Task-kind registry and image argv (契约 case-model.md §4)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vrgen.errors import MglcError, UsageError
from vrgen.platform.client import Mglc
from vrgen.round.image import IMAGE_TIERS, build_image_argv
from vrgen.round.kinds import KINDS


def test_registry_declares_both_kinds():
    assert set(KINDS) == {"video", "image"}
    video, image = KINDS["video"], KINDS["image"]
    assert video.quota_command == ("video", "generate")
    assert image.quota_command == ("image", "generate")
    assert video.needs_virtual_ip is True
    assert image.needs_virtual_ip is False
    assert video.multi_result is False
    assert image.multi_result is True
    assert video.resolution_tiers == ("480p", "720p", "1080p")
    assert image.resolution_tiers == IMAGE_TIERS
    for kind in KINDS.values():
        assert callable(kind.build_argv)
        assert callable(kind.prepare_round)
        assert callable(kind.execute_round)
        assert callable(kind.gate)


def test_build_image_argv_shape():
    argv = build_image_argv(prompt="p", model_id=44,
                            images=["a.png", "b.png"], ratio="16:9",
                            resolution="4K", num=4, session_id=9)
    joined = " ".join(argv)
    assert "--ratio 16:9" in joined and "--resolution 4K" in joined
    assert joined.count("--image ") == 2
    assert argv.index("a.png") < argv.index("b.png")
    assert "--session-id 9" in joined
    with pytest.raises(UsageError, match="1K"):
        build_image_argv(prompt="p", model_id=44, images=["a.png"],
                         ratio="16:9", resolution="1080p", num=1)


def test_client_image_generate_requires_confirm():
    calls: list[list[str]] = []

    def runner(argv, timeout):
        calls.append(list(argv))
        return type("P", (), {"stdout": json.dumps({"code": 0, "data": {"taskId": 7}}),
                              "returncode": 0, "stderr": ""})()

    client = Mglc(binary="mglc-fake", runner=runner)
    with pytest.raises(MglcError) as excinfo:
        client.image_generate(["--prompt", "p"], confirm=False)
    assert "confirm" in str(excinfo.value)
    assert calls == []
    client.image_generate(["--prompt", "p"], confirm=True)
    assert calls and calls[0][1:3] == ["image", "generate"]


def test_kind_of_reads_ledger_kind():
    from vrgen.round.kinds import kind_of
    from vrgen.round.ledger import Run

    run = Run(key="k", dir=Path("."), params={"kind": "image"})
    assert kind_of(run).kind == "image"
    assert kind_of(Run(key="k", dir=Path("."))).kind == "video"
