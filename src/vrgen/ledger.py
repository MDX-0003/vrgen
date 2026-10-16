"""轮次台账 run.json 的读写。

格式是对外契约（E:\\VRAIGC\\docs\\case-model.md §5：stage/params/prompt/assets/
task_id/result/gate/notes 五件套之首）。M3 实现 manual 后端所需的最小集；
M1 迁入 vrpad 的 round/ledger 时必须保持本格式，不得另起炉灶。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path

from .errors import VrgenError

SCHEMA = 1
RUN_JSON = "run.json"
RESULT_MP4 = "result.mp4"


def now_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class RunRecord:
    """一轮的全部台账事实。manual 与 mglc 后端共用（manual 时 prompt/task_id
    为 None、assets 为空，其余字段语义一致）。"""

    job: str
    round: str
    backend: str                       # manual | mglc
    stage: str                         # done | failed（mglc 另有 running 等中间态）
    created_utc: str
    params: dict = field(default_factory=dict)
    prompt: dict | None = None
    assets: list = field(default_factory=list)
    task_id: str | None = None
    result: dict = field(default_factory=dict)   # {path, source, bytes, sha256}
    gate: dict = field(default_factory=dict)     # {verdict: ok|skipped|error, ...}
    notes: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["schema"] = SCHEMA
        return d

    def save(self, round_dir: str | Path) -> Path:
        path = Path(round_dir) / RUN_JSON
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
        return path

    @staticmethod
    def load(round_dir: str | Path) -> RunRecord:
        path = Path(round_dir) / RUN_JSON
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise VrgenError(f"{path} 不存在——这一轮没有台账") from exc
        except json.JSONDecodeError as exc:
            raise VrgenError(f"{path} 不是合法 JSON：{exc}") from exc
        if int(raw.get("schema", 0)) != SCHEMA:
            raise VrgenError(f"{path}: 不支持的台账 schema {raw.get('schema')!r}")
        known = {f.name for f in fields(RunRecord)} | {"schema"}
        missing = known - {"schema"} - set(raw)
        if missing:
            raise VrgenError(f"{path}: 台账缺字段 {sorted(missing)}")
        return RunRecord(
            job=str(raw["job"]), round=str(raw["round"]), backend=str(raw["backend"]),
            stage=str(raw["stage"]), created_utc=str(raw["created_utc"]),
            params=dict(raw.get("params", {})),
            prompt=raw.get("prompt"), assets=list(raw.get("assets", [])),
            task_id=raw.get("task_id"), result=dict(raw.get("result", {})),
            gate=dict(raw.get("gate", {})), notes=str(raw.get("notes", "")),
        )
