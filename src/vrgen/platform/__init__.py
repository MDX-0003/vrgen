"""The only layer that talks to the outside: a subprocess and one public GET.

Everything here is replaceable in tests (``Mglc(runner=...)``,
``load_models(fetcher=...)``), which is what keeps the rest of the package honest
about being pure.
"""

from __future__ import annotations

from .binpath import ENV_VAR, find_mglc, install_hint, require_mglc
from .client import Envelope, Mglc, subprocess_runner
from .models import (
                     CACHE_NAME,
                     MODEL_LIST_URL,
                     ModeLimits,
                     ModelInfo,
                     cache_path,
                     fetch_items,
                     find_model,
                     load_models,
                     parse_models,
)

__all__ = [
    "ENV_VAR", "find_mglc", "install_hint", "require_mglc",
    "Envelope", "Mglc", "subprocess_runner",
    "CACHE_NAME", "MODEL_LIST_URL", "ModeLimits", "ModelInfo", "cache_path",
    "fetch_items", "find_model", "load_models", "parse_models",
]
