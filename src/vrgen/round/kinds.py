"""Task-kind registry: what each mglc generation capability needs.

The video and image subcommands do not share a parameter vocabulary, so a single
function with an ``if kind`` chain would grow a branch per capability. Each kind
declares its subcommand, argv builder, lifecycle entry points, result shape and
gate here; the CLI looks the kind up instead of branching on a string.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..manual import probe_gate
from ..prep.params import OUTPUT_TIERS
from . import image as image_mod
from . import pipeline, task
from .ledger import Run


@dataclass(frozen=True)
class TaskKind:
    kind: str
    quota_command: tuple[str, ...]
    build_argv: Callable
    prepare_round: Callable
    execute_round: Callable
    needs_virtual_ip: bool
    multi_result: bool
    result_glob: str
    resolution_tiers: tuple[str, ...]
    gate: Callable


KINDS: dict[str, TaskKind] = {
    "video": TaskKind(
        kind="video",
        quota_command=("video", "generate"),
        build_argv=task.build_generate_argv,
        prepare_round=pipeline.prepare_round,
        execute_round=pipeline.execute_round,
        needs_virtual_ip=True,
        multi_result=False,
        result_glob="result.*",
        resolution_tiers=OUTPUT_TIERS,
        gate=probe_gate,
    ),
    "image": TaskKind(
        kind="image",
        quota_command=("image", "generate"),
        build_argv=image_mod.build_image_argv,
        prepare_round=image_mod.prepare_image_round,
        execute_round=image_mod.execute_image_round,
        needs_virtual_ip=False,
        multi_result=True,
        result_glob="result_*.png",
        resolution_tiers=image_mod.IMAGE_TIERS,
        gate=image_mod.gate_image,
    ),
}


def kind_of(run: Run) -> TaskKind:
    name = run.kind
    if name not in KINDS:
        raise KeyError(f"unknown task kind {name!r}")
    return KINDS[name]
