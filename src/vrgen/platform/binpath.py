"""Locate the ``mglc`` binary.

Deliberately **not** in ``binres.py``: everything there is addressed relative to the
repo root because it belongs to the shipped pixel baseline (ffmpeg/ffprobe, whose
build is part of what a result means). ``mglc`` is different in kind -- it needs a
human's credentials, it talks to a remote service, and a different version of it
changes nothing about the geometry. So it resolves from the environment first, and
``binres`` is left alone.

Order:

1. ``$VRPAD_MGLC_BIN``  -- explicit override, and the injection point tests use
2. ``aigc.mglc_bin``    -- config, absolute or relative to the repo root
3. ``PATH``             -- whatever the vendor installer put there
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from ..errors import MglcNotFound
from ..paths import repo_root

ENV_VAR = "VRGEN_MGLC_BIN"


def install_hint() -> str:
    return ("Install it with the vendor script, then make sure it is on PATH:\n"
            "  curl -fsSL https://aigc-assets.mgtv.com/mglc/install.sh | bash\n"
            "Windows: download mglc-<version>-windows-amd64.zip from\n"
            "  https://aigc-assets.mgtv.com/mglc/v<version>/  (verify against checksums.txt)\n"
            f"or point {ENV_VAR} at the executable.")


def find_mglc(config=None, env: dict | None = None) -> str | None:
    env = os.environ if env is None else env

    override = env.get(ENV_VAR)
    if override:
        candidate = Path(override)
        if candidate.is_file():
            return str(candidate)

    configured = config.get("mglc.mglc_bin") if config is not None else None
    if configured:
        candidate = Path(str(configured))
        if not candidate.is_absolute():
            candidate = repo_root() / candidate
        if candidate.is_file():
            return str(candidate)

    for name in ("mglc", "mglc.exe"):
        found = shutil.which(name)
        if found:
            return found
    return None


def require_mglc(config=None, env: dict | None = None) -> str:
    path = find_mglc(config, env)
    if path:
        return path
    raise MglcNotFound(f"mglc not found (looked at ${ENV_VAR}, aigc.mglc_bin, PATH).\n"
                       + install_hint())
