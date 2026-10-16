"""控制台输出助手（← vrpad progress.py 的 Log，vrgen 自用副本）。"""

from __future__ import annotations

import sys
from pathlib import Path


class Log:
    """Level-prefixed console log, optionally teed into a file.

    ``attach`` exists because the submit wait runs for minutes and the terminal
    is not a record. The file gets every line regardless of ``quiet``.
    """

    def __init__(self, quiet: bool = False, stream=None, file: str | Path | None = None):
        self.quiet = quiet
        self.stream = stream or sys.stderr
        self._file = None
        if file is not None:
            self.attach(file)

    def attach(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("a", encoding="utf-8")
        return path

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None

    def _emit(self, prefix: str, message: str) -> None:
        line = f"{prefix} {message}"
        if self._file is not None:
            self._file.write(line + "\n")
            self._file.flush()
        if self.quiet and prefix not in ("ERROR", "WARN"):
            return
        print(line, file=self.stream, flush=True)

    def info(self, message: str) -> None:
        self._emit("  ", message)

    def step(self, message: str) -> None:
        self._emit("->", message)

    def warn(self, message: str) -> None:
        self._emit("WARN", message)

    def error(self, message: str) -> None:
        self._emit("ERROR", message)
