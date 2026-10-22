"""异常族与退出码（契约：docs/case-model.md §4）。

- ``UsageError``   → 退出码 2（参数或配置错，还没碰任何数据）
- ``VrgenError``   → 退出码 1（运行失败；台账里应有现场）

``AigcError`` 系（← vrpad aigc/errors.py 原样迁入）挂在 ``VrgenError`` 之下，
迁移代码照旧 raise，CLI 统一按 VrgenError 捕获。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


class VrgenError(RuntimeError):
    """运行失败，退出码 1。"""


class UsageError(VrgenError):
    """参数/配置错误，退出码 2。"""


class AigcError(VrgenError):
    """Base class: something about the AIGC handoff is wrong."""


class MglcNotFound(AigcError):
    """The ``mglc`` binary is not where we can find it."""


class MglcProtocolError(AigcError):
    """``mglc`` answered with something that is not the documented JSON envelope."""


class MglcError(AigcError):
    """``mglc`` reported a non-zero ``code``.

    Carries the envelope so a caller can branch on ``code`` -- the CLI's process
    exit code is 0/1 only and is not an error signal (an unknown subcommand exits
    0 with help text on stdout).
    """

    def __init__(self, code: int, msg: str, argv: Sequence[str] = (), raw: str = ""):
        self.code = int(code)
        self.msg = str(msg)
        self.argv = tuple(argv)
        self.raw = raw
        super().__init__(f"mglc code {self.code}: {self.msg or '(no message)'}"
                         + (f"\n  command: {' '.join(self.argv)}" if self.argv else ""))


class NotLoggedIn(MglcError):
    """code 10002 -- no credentials. The fix is a human action, not a retry."""


class BudgetError(AigcError):
    """The artifact does not fit the model's published input limits."""


class LedgerError(AigcError):
    """A run-ledger problem: a duplicate spend, a missing run, a key collision."""


class AssetError(AigcError):
    """The SD-compliance asset library refused, or never approved, an input."""


class TaskFailed(AigcError):
    """The generation task reached a terminal non-success state.

    ``reason`` 是平台返回的具体失败原因（task status 的 ``failReason``）；
    有它就不再只显示一个 'failed'，台账和 web 任务日志都能直接看到原因。
    """

    def __init__(self, task_id: Any, state: str, raw: str = "", reason: str = ""):
        self.task_id = task_id
        self.state = state
        self.raw = raw
        self.reason = str(reason or "")
        detail = self.reason or raw
        super().__init__(f"task {task_id} ended as {state!r}"
                         + (f": {detail}" if detail else ""))
