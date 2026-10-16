"""Pure layers of the AIGC handoff: limits, upload sizing, prompt rendering.

Nothing here touches the network, starts a process, or writes a file, so all of it
is testable offline and none of it can spend quota. If a module in this package
needs to know something about the outside world, it must be handed that knowledge.
"""

from __future__ import annotations

from .budget import BudgetReport, Check, InputSpec, check_input, even_down, suggest_size
from .prompt import (
                     GEOMETRY_KEYS,
                     geometry_variables,
                     hash_prompt,
                     load_brief,
                     render,
                     render_files,
                     resolve_template,
)
from .scale import SIZE_MODES, UploadGeometry, choose_upload_size, parse_size_mode, scaled_geometry

__all__ = [
    "BudgetReport", "Check", "InputSpec", "check_input", "even_down", "suggest_size",
    "GEOMETRY_KEYS", "geometry_variables", "hash_prompt", "load_brief", "render",
    "render_files", "resolve_template",
    "SIZE_MODES", "UploadGeometry", "choose_upload_size", "parse_size_mode",
    "scaled_geometry",
]
