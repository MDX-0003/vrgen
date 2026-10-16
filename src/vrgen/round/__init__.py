"""One generation round: its ledger, its assets, its task, and the gate.

The stage order is fixed and is also the CLI's:

    prepare -> assets -> submit -> status -> fetch -> check -> (recompose)

Only ``submit`` spends quota, and it will not run without an explicit confirmation
plus a ledger that says this exact request has not been sent before.

（gate 不随迁：几何闸门归 vrstudio 的 recompose，决策 D3；vrgen 末段是通用闸门。）
"""

from __future__ import annotations

from .assets import (
                     ACTIVE_STATES,
                     FAILED_STATES,
                     KIND_IMAGE,
                     KIND_VIDEO,
                     asset_state,
                     cached,
                     classify,
                     ensure_assets,
                     extract_asset_id,
                     status_of,
                     upload,
                     wait_active,
)
from .ledger import (
                     CALL_LOG,
                     COMMAND_FILE,
                     PROMPT_FILE,
                     RUN_FILE,
                     STAGES,
                     Run,
                     RunStore,
                     append_call_log,
                     assert_spendable,
                     run_key,
                     shell_line,
                     write_command,
)
from .pipeline import Outcome, Prepared, execute_round, prepare_round
from .task import (
                     TASK_STATES,
                     TERMINAL_BAD,
                     TERMINAL_GOOD,
                     build_generate_argv,
                     extract_task_id,
                     fetch,
                     parse_state,
                     result_urls,
                     state,
                     submit,
                     wait,
)

__all__ = [
    "ACTIVE_STATES", "FAILED_STATES", "KIND_IMAGE", "KIND_VIDEO", "asset_state", "cached",
    "classify", "ensure_assets", "extract_asset_id", "status_of", "upload", "wait_active",
    "CALL_LOG", "COMMAND_FILE", "PROMPT_FILE", "RUN_FILE", "STAGES", "Run", "RunStore",
    "append_call_log", "assert_spendable", "run_key", "shell_line", "write_command",
    "TASK_STATES", "TERMINAL_BAD", "TERMINAL_GOOD", "build_generate_argv",
    "extract_task_id", "fetch", "parse_state", "result_urls", "state", "submit", "wait",
    "Outcome", "Prepared", "execute_round", "prepare_round", "InputPackage",
]
