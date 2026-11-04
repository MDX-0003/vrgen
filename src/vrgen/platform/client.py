"""Run ``mglc`` and make sense of its answer.

Two facts about this CLI drive the whole module:

* **Every output goes to stdout, including errors.** So a failure and a success are
  distinguished by parsing the JSON envelope's ``code``, not by the exit status or
  the stream.
* **The process exit code is 0/1 and cannot be trusted** -- an unknown subcommand
  exits 0 having printed help text. Anything that branches on the exit code will
  silently treat "you typed the subcommand wrong" as success.

The ``Runner`` indirection exists so the whole ``round/`` layer can be tested with
no binary, no credentials, and no network.

Only ``generate()`` spends quota, it must be asked for explicitly (``confirm``),
and it is recorded in ``transcript`` so a caller can prove what was sent.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, timedelta, timezone
from pathlib import Path
from typing import Any

from ..errors import MglcError, MglcNotFound, MglcProtocolError, NotLoggedIn
from .binpath import require_mglc

NOT_LOGGED_IN = 10002
BAD_CREDENTIALS = -401

#: mglc.log 的 `[...]` 行是给人复盘用的，用东八区。时刻本身带时区信息（``isoformat``
#: 直接给出 ``+08:00`` 偏移），读到的人不用猜、也不用做加减法换算。
LOG_TZ = timezone(timedelta(hours=8))


def _now_log() -> str:
    from datetime import datetime
    return datetime.now(LOG_TZ).isoformat(timespec="seconds")


def subprocess_runner(argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(list(argv), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


@dataclass(frozen=True)
class Envelope:
    """One ``mglc`` invocation's result, in the tool's own terms."""

    argv: tuple[str, ...]
    code: int
    data: Any
    msg: str
    stdout: str
    utc: str = ""                       # when the call was made, for the round log (UTC+8)

    @property
    def ok(self) -> bool:
        return self.code == 0

    @property
    def text(self) -> str:
        """Raw stdout, for a caller that asked for ``--text-plain``."""
        return self.stdout.strip()


@dataclass
class Mglc:
    """A thin, testable wrapper: argv in, :class:`Envelope` out."""

    binary: str | None = None
    runner: Callable[..., subprocess.CompletedProcess] = subprocess_runner
    config: Any = None
    timeout: float = 120.0
    env: dict | None = None
    transcript: list[Envelope] = field(default_factory=list)

    # ---- plumbing ------------------------------------------------------
    def exe(self) -> str:
        """Resolved lazily, so an injected runner never needs a real binary."""
        if self.binary:
            return self.binary
        self.binary = require_mglc(self.config, self.env)
        return self.binary

    def call(self, *args: str, timeout: float | None = None,
             allow_text: bool = False) -> Envelope:
        argv = [self.exe(), *args]
        try:
            proc = self.runner(argv, timeout if timeout is not None else self.timeout)
        except FileNotFoundError as exc:
            raise MglcNotFound(f"could not execute {argv[0]}: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise MglcError(-1, f"timed out after {exc.timeout:g}s", argv) from exc

        stdout = proc.stdout or ""
        payload = _parse_envelope(stdout)
        if payload is None:
            if allow_text:
                env = Envelope(tuple(argv), 0, None, "", stdout, _now_log())
                self.transcript.append(env)
                return env
            head = stdout.strip()[:300] or (proc.stderr or "").strip()[:300]
            raise MglcProtocolError(
                f"mglc did not answer with JSON (exit {proc.returncode}):\n{head}\n"
                f"  command: {' '.join(argv)}\n"
                f"  (an unknown subcommand prints help text and still exits 0)"
            )

        env = Envelope(tuple(argv), int(payload.get("code", 0)), payload.get("data"),
                       str(payload.get("msg", "")), stdout, _now_log())
        self.transcript.append(env)
        if not env.ok:
            raise _as_error(env)
        return env

    # ---- commands (all read-only unless noted) --------------------------
    def version(self) -> dict:
        return self.call("version").data or {}

    def status(self) -> Envelope:
        return self.call("status")

    def user_info(self) -> Envelope:
        return self.call("user", "info")

    def model_list(self, model_type: str = "video", **extra: Any) -> Envelope:
        return self.call("model", "list", "--type", model_type, *_flags(extra))

    def model_detail(self, model_id: int, model_type: str = "video") -> Envelope:
        return self.call("model", "--type", model_type, "--model-id", str(int(model_id)))

    def project_list(self) -> Envelope:
        return self.call("project", "list")

    def episode_list(self, project_id: int) -> Envelope:
        return self.call("episode", "list", "--project-id", str(int(project_id)))

    def session_list(self, project_id: int, episode: int) -> Envelope:
        return self.call("session", "list", "--project-id", str(int(project_id)),
                         "--episode", str(int(episode)))

    def task_status(self, task_id: int) -> Envelope:
        return self.call("task", "status", "--task-id", str(int(task_id)))

    def _spending(self, command: Sequence[str], args: Sequence[str], *,
                  confirm: bool, timeout: float) -> Envelope:
        """The one guard around every quota-spending subcommand."""
        if not confirm:
            raise MglcError(
                -1,
                f"{' '.join(command)} needs confirm=True; use `vrgen ... --yes`",
                [*command, *args])
        return self.call(*command, *args, timeout=timeout)

    def generate(self, args: Sequence[str], confirm: bool = False,
                 timeout: float = 300.0) -> Envelope:
        """THE video quota-spending call. Refuses unless ``confirm`` is true.

        ``args`` is built by ``round.task.build_generate_argv`` -- one place decides
        what a request looks like, so there is one place to audit.
        """
        return self._spending(("video", "generate"), args,
                              confirm=confirm, timeout=timeout)

    def image_generate(self, args: Sequence[str], confirm: bool = False,
                       timeout: float = 300.0) -> Envelope:
        """THE image quota-spending call; same confirmation rule as video."""
        return self._spending(("image", "generate"), args,
                              confirm=confirm, timeout=timeout)


def walk_scalars(node: Any):
    """Yield ``(key, value)`` for every scalar in a nested payload.

    Response shapes are only documented loosely (and not at all for the
    ``/openapi/v1/cli/*`` surface this CLI uses), so the readers search the payload
    by key name instead of indexing a path, and raise with the raw JSON when they
    find nothing. One shared walker keeps every reader's tolerance identical.
    """
    if isinstance(node, Mapping):
        for key, value in node.items():
            if isinstance(value, (Mapping, list, tuple)):
                yield from walk_scalars(value)
            else:
                yield str(key), value
    elif isinstance(node, (list, tuple)):
        for item in node:
            yield from walk_scalars(item)


def _flags(extra: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for key, value in extra.items():
        flag = "--" + key.replace("_", "-")
        if isinstance(value, bool):
            if value:
                out.append(flag)
        elif value is not None:
            out += [flag, str(value)]
    return out


def _parse_envelope(stdout: str) -> dict | None:
    """The envelope is a single JSON object; tolerate leading/trailing noise."""
    text = stdout.strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            payload = json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    return payload if isinstance(payload, dict) and "code" in payload else None


def _as_error(env: Envelope) -> MglcError:
    if env.code == NOT_LOGGED_IN:
        # No `doctor` command exists any more, so the fix has to be in the message.
        return NotLoggedIn(
            env.code, (env.msg or "not logged in")
            + "\n  run one of:\n"
              "    mglc auth --source workbuddy            # device code, approve in a browser\n"
              "    mglc login --access-key <AK> --secret-key <SK>\n"
              "  (credentials: https://aigc.mgtv.com/develop/media/api-keys, up to 3 pairs)",
            env.argv, env.stdout)
    if env.code == BAD_CREDENTIALS:
        return MglcError(env.code, (env.msg or "bad credentials")
                         + "\n  check aigc.mglc_bin is not pointing at a wrapper that "
                           "drops env vars, and re-run `mglc login`", env.argv, env.stdout)
    return MglcError(env.code, env.msg, env.argv, env.stdout)
