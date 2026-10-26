"""The run ledger: one directory per round, and the key that makes it idempotent.

A generation round is not a free operation and cannot be cancelled, so the ledger
exists to answer one question before anything is spent: *have we already sent
exactly this?* The key is a hash of the rendered prompt plus the hashes of the two
inputs plus every generation parameter, so "the script crashed and I re-ran it"
resolves to the same key and is refused instead of billed twice.

The run directory is also the record. Copy it somewhere and you have the whole
provenance of one round: prompt, upload artifacts, the exact argv, the task id,
the model's answer, the gate metrics. That is deliberately the same philosophy as
``manifest.py`` -- the artifact set is the contract.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC
from pathlib import Path
from typing import Any

from ..errors import LedgerError
from ..paths import sha256_16
from ..prep.prompt import hash_prompt

#: Lifecycle. Forward-only in practice; ``failed`` is the one off-ramp.
STAGES = ("prepared", "assets_ready", "submitted", "succeeded", "failed",
          "fetched", "checked")

RUN_FILE = "run.json"
PROMPT_FILE = "prompt.txt"
COMMAND_FILE = "command.txt"
CALL_LOG = "mglc.log"
PROGRESS_LOG = "progress.log"


@dataclass
class Run:
    key: str
    dir: Path
    stage: str = "prepared"
    params: dict[str, Any] = field(default_factory=dict)
    prompt: str = ""
    prompt_hash: str = ""
    upload: dict[str, Any] = field(default_factory=dict)        # the video we send
    mask: dict[str, Any] | None = None
    assets: dict[str, int] = field(default_factory=dict)        # kind -> asset id
    task_id: int | None = None
    task_ids: list[int] | None = None      # image batches: one id per sibling task
    result: str | None = None
    results: list[str] | None = None       # image rounds: every candidate
    gate: dict[str, Any] | None = None
    created_utc: str = ""
    updated_utc: str = ""
    notes: list[str] = field(default_factory=list)
    spend_guard: bool = True

    # ---- serialisation -------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        out = dict(self.__dict__)
        out["dir"] = str(self.dir)
        return out

    def save(self) -> Path:
        self.updated_utc = _now()
        path = self.dir / RUN_FILE
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False),
                        encoding="utf-8")
        return path

    @staticmethod
    def load(target: str | Path) -> Run:
        target = Path(target)
        path = target / RUN_FILE if target.is_dir() else target
        if not path.is_file():
            raise LedgerError(f"no run ledger at {path}")
        blob = json.loads(path.read_text(encoding="utf-8"))
        blob["dir"] = Path(blob["dir"])
        return Run(**blob)

    # ---- convenience ---------------------------------------------------
    @property
    def prompt_path(self) -> Path:
        return self.dir / PROMPT_FILE

    @property
    def result_path(self) -> Path | None:
        return Path(self.result) if self.result else None

    @property
    def kind(self) -> str:
        """``video`` unless the round explicitly says otherwise."""
        return str(self.params.get("kind") or "video")

    def note(self, text: str) -> None:
        self.notes.append(text)

    def advance(self, stage: str) -> None:
        if stage not in STAGES:
            raise LedgerError(f"unknown stage {stage!r}; known: {', '.join(STAGES)}")
        self.stage = stage


def run_key(prompt: str, upload: str | Path, mask: str | Path | None = None,
            params: Mapping[str, Any] | None = None,
            extra: Mapping[str, Any] | None = None) -> str:
    """Spend-once key: identical prompt + identical inputs + identical parameters.

    ``extra`` carries the gen multi-asset fingerprints (one hash per upload) so two
    rounds that differ only in a secondary video cannot collide.
    """
    blob = {
        "prompt": hash_prompt(prompt),
        "upload": sha256_16(upload),
        "mask": sha256_16(mask) if mask else None,
        "params": {str(k): params[k] for k in sorted(params or {})},
        "extra": dict(extra or {}),
    }
    canonical = json.dumps(blob, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def failed_round_hint(run: Run) -> str:
    """How to retry a round whose task already died. Used by both refusal paths."""
    return ("  that attempt failed. To send the same prompt again, move the run\n"
            f"  directory aside and re-run -- the old task may still be billed, so\n"
            f"  this is not done automatically:\n"
            f"    move {run.dir} {run.dir.with_name(run.dir.name + '-failed')}")


def assert_spendable(run: Run, *, force: bool = False) -> None:
    """Refuse to spend again on a round that already went out.

    Called by ``task.submit`` -- the function that spends is the function that guards,
    so a direct library call cannot double-charge either. In the normal flow this is
    never reached: the pipeline only submits when the ledger has no task id, and the
    recovery from a dead task is a deliberate act (moving the record aside) rather
    than a flag, because a failed task may still have been billed.
    """
    if run.task_id is None or not run.spend_guard:
        return
    if force:
        run.note(f"resubmitted over task {run.task_id} (force)")
        return
    if run.stage == "failed":
        advice = failed_round_hint(run)
    else:
        advice = ("  it is either still running or already done. Re-running this command\n"
                  "  waits for / reuses it; to start over, move the run directory aside.")
    raise LedgerError(
        f"this round was already submitted as task {run.task_id} (stage {run.stage}).\n"
        f"  key: {run.key}\n"
        f"  Submitting again would spend quota a second time for the same prompt and inputs,\n"
        f"  so it is refused:\n" + advice
    )


class RunStore:
    """Runs live in ``<workdir>/<label>-<key[:8]>/``.

    The directory name carries the theme so ``dir runs`` answers "which rounds do I
    have" without opening anything. The full key stays inside ``run.json`` and is what
    lookups match on -- the name is for humans, the key is the identity.
    """

    def __init__(self, workdir: str | Path):
        self.workdir = Path(workdir)

    def dir_for(self, key: str, label: str | None = None) -> Path:
        return self.workdir / (f"{label}-{key[:8]}" if label else key[:12])

    def create(self, key: str, *, label: str | None = None,
               params: Mapping[str, Any] | None = None,
               prompt: str = "", upload: str | Path | None = None,
               mask: str | Path | None = None) -> Run:
        # Identity, not the directory name, decides whether this round already exists:
        # creating the same key under a different label would otherwise slip through.
        existing = self.find_by_key(key)
        if existing is not None:
            raise LedgerError(
                f"a run with this key already exists: {existing.dir}\n"
                f"  load it with `vrpad aigc --run {existing.dir}`"
            )
        directory = self.dir_for(key, label)
        directory.mkdir(parents=True, exist_ok=True)
        run = Run(key=key, dir=directory, params=dict(params or {}), prompt=prompt,
                  prompt_hash=hash_prompt(prompt), created_utc=_now(), updated_utc=_now())
        if upload:
            run.upload = {"path": str(upload)}
        if mask:
            run.mask = {"path": str(mask)}
        run.prompt_path.write_text(prompt, encoding="utf-8")
        run.save()
        return run

    def find_by_key(self, key: str) -> Run | None:
        for run in self.all():
            if run.key == key:
                return run
        return None

    def open(self, target: str | Path) -> Run:
        """Accept a run directory, a ``run.json``, or a key (or key prefix)."""
        target = Path(target)
        if target.is_dir() or target.is_file():
            return Run.load(target)
        if target.parent != Path(".") and (target.parent / RUN_FILE).is_file():
            return Run.load(target.parent)
        prefix = str(target)
        matches = [r for r in self.all() if r.key.startswith(prefix)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise LedgerError(f"{prefix!r} matches {len(matches)} runs under {self.workdir}; "
                              f"give a longer prefix or the run directory")
        raise LedgerError(f"no run matching {target} under {self.workdir}")

    def all(self) -> list[Run]:
        if not self.workdir.is_dir():
            return []
        runs = []
        for child in sorted(self.workdir.iterdir()):
            if (child / RUN_FILE).is_file():
                try:
                    runs.append(Run.load(child))
                except (LedgerError, json.JSONDecodeError, TypeError,
                        KeyError, ValueError):
                    continue          # 坏/旧格式台账不阻塞列举，但也不假装它存在
        return runs

    def summary(self, limit: int = 6) -> list[str]:
        """``["dusk checked", "storm prepared"]`` -- the 'where am I' line.

        This is what replaces a listing command: the rounds are printed on every
        invocation, and the directories are readable, so nothing has to be asked for.
        """
        rows = []
        for run in self.all():
            brief = str(run.params.get("brief") or "")
            label = Path(brief).stem if brief else run.dir.name
            rows.append(f"{label} {run.stage}")
        return rows[-limit:]


def _now() -> str:
    from datetime import datetime
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def shell_line(argv: Sequence[str]) -> str:
    """One copy-pastable command line, quoting only what needs it."""
    out: list[str] = []
    for token in argv:
        text = str(token)
        out.append(f'"{text}"' if any(ch in text for ch in ' "') else text)
    return " ".join(out)


def write_command(run: Run, argv: Sequence[str], *, spend: bool) -> Path:
    """Keep the exact invocation next to the run: `mglc ...` in one copy-pastable line."""
    path = run.dir / COMMAND_FILE
    header = "# spends quota\n" if spend else "# read-only\n"
    path.write_text(header + shell_line(list(argv)) + "\n", encoding="utf-8")
    return path


def append_call_log(run: Run, calls: Sequence, *, mode: str) -> Path:
    """Append one invocation's external calls to the round's log.

    **Appending, not overwriting.** A round is normally attempted more than once --
    interrupted, quota refused, account switched -- and the point of a log is to show
    what *each* attempt did, failures included. The ledger's ``notes`` only records
    what succeeded, which is exactly why the first "asset does not belong to this
    enterprise" was invisible afterwards.

    ``calls`` are :class:`~vrpad.aigc.platform.client.Envelope` objects; only the
    fields worth re-reading by eye are written (when, code, argv, message).
    """
    path = run.dir / CALL_LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"=== {_now()}  {mode}"]
    if not calls:
        lines.append("    (no external calls)")
    for envelope in calls:
        lines.append(f"[{getattr(envelope, 'utc', '') or '?'}] "
                     f"code={envelope.code}  {shell_line(envelope.argv)}")
        if envelope.msg:
            lines.append(f"    msg: {envelope.msg}")
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    return path
