"""错误类型与退出码的对应（契约：docs/case-model.md §4）。

- ``UsageError``   → 退出码 2（参数或配置错，还没碰任何数据）
- ``VrgenError``   → 退出码 1（运行失败；台账里应有现场）
"""


class VrgenError(Exception):
    """运行失败，退出码 1。"""


class UsageError(VrgenError):
    """参数/配置错误，退出码 2。"""
