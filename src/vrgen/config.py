"""分层配置（[mglc] 段 ← vrpad [aigc] 段迁入，键名不变）。

优先级从高到低：``--set key=value`` > ``--config FILE`` > ``./vrgen.toml`` >
``~/.config/vrgen/default.toml`` > 仓库 ``config/default.toml``（内置默认）。
未知键是硬错误（沿 vrpad 哲学：拼错的 --set 静默无效是最坏的失败模式）。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .errors import UsageError
from .paths import CONFIG_DIR

DEFAULTS_FILE = CONFIG_DIR / "default.toml"

_FALLBACK: dict = {
    "schema": 1,
    "mglc": {
        "video_model_id": 37,
        "mode": "videoEdit",
        "resolution": "1080p",
        "duration_s": 0,
        "aspect_ratio": "21:9",
        "num": 1,
        "keep_original_sound": True,
        "session_id": 0,
        "scale": "auto",
        "upload_crf": 16,
        "prompt_template": "360-closure",
        "mglc_bin": "",
        "model_list_url": "https://aigc.mgtv.com/api/v1/aitools/videoModelList",
        "models_max_age_h": 24.0,
        "poll_interval_s": 20.0,
        "poll_timeout_s": 3600.0,
        "review_timeout_s": 1800.0,
        "image_model_id": 44,
        "image_ratio": "16:9",
        "image_resolution": "4K",
        "image_num": 4,
    },
}


class ConfigError(UsageError):
    pass


@dataclass(frozen=True)
class Config:
    data: dict
    source_path: Path | None = None

    def get(self, path: str, default=None):
        node = self.data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


def _deep_merge(base: dict, overlay: dict, origin: str) -> dict:
    out = dict(base)
    for key, value in overlay.items():
        if key not in out:
            raise ConfigError(f"{origin}: unknown config key {key!r}")
        if isinstance(value, dict):
            if not isinstance(out[key], dict):
                raise ConfigError(
                    f"{origin}: {key!r} is a table in the file but a scalar in defaults")
            out[key] = _deep_merge(out[key], value, f"{origin}:{key}")
        else:
            out[key] = value
    return out


def parse_override(text: str) -> tuple[str, object]:
    """``"mglc.resolution=720p"`` → ``("mglc.resolution", "720p")``。

    值按 TOML 解析（数字/布尔/数组按文件语义）；不是合法 TOML 的裸词当字符串。
    """
    key, sep, raw = text.partition("=")
    if not sep:
        raise ConfigError(f"--set needs key=value, got {text!r}")
    literal = raw.strip()
    if not literal:
        raise ConfigError(f"--set {key}: empty value")
    try:
        value: object = tomllib.loads(f"v = {literal}")["v"]
    except tomllib.TOMLDecodeError:
        value = literal
    return key.strip(), value


def _set_path(data: dict, key: str, value: object) -> None:
    parts = key.split(".")
    node = data
    for part in parts[:-1]:
        if part not in node or not isinstance(node[part], dict):
            raise ConfigError(f"--set {key}: no such config section {part!r}")
        node = node[part]
    if parts[-1] not in node:
        raise ConfigError(f"--set {key}: unknown config key")
    node[parts[-1]] = value


def builtin_defaults() -> dict:
    if DEFAULTS_FILE.is_file():
        with open(DEFAULTS_FILE, "rb") as fh:
            return tomllib.load(fh)
    return _FALLBACK


def _absorb_legacy_model_key(user: dict) -> dict:
    """旧配置键 mglc.model_id → mglc.video_model_id（用户没写新键时）。"""
    mglc = user.get("mglc")
    if isinstance(mglc, dict) and "model_id" in mglc and "video_model_id" not in mglc:
        mglc["video_model_id"] = mglc.pop("model_id")
    return user


def load(config_path: str | Path | None = None,
         overrides: list[str] = (),
         cwd: str | Path | None = None,
         home: str | Path | None = None) -> Config:
    data = builtin_defaults()
    cwd = Path(cwd) if cwd else Path.cwd()
    home = Path(home) if home else Path(os.path.expanduser("~"))

    for candidate in (home / ".config" / "vrgen" / "default.toml", cwd / "vrgen.toml"):
        if candidate.is_file():
            with open(candidate, "rb") as fh:
                user = _absorb_legacy_model_key(tomllib.load(fh))
                data = _deep_merge(data, user, str(candidate))
    if config_path:
        path = Path(config_path)
        if not path.is_file():
            raise ConfigError(f"config file not found: {path}")
        with open(path, "rb") as fh:
            user = _absorb_legacy_model_key(tomllib.load(fh))
            data = _deep_merge(data, user, str(path))
    else:
        path = None

    for override in overrides:
        key, value = parse_override(override)
        _set_path(data, key, value)

    _validate(data)
    return Config(data=data, source_path=path)


def _validate(data: dict) -> None:
    if int(data.get("schema", 0)) != 1:
        raise ConfigError(f"unsupported config schema {data.get('schema')!r} (expected 1)")
    mglc = data["mglc"]
    if int(mglc["video_model_id"]) < 1:
        raise ConfigError("mglc.video_model_id must be >= 1 (it is a platform model code)")
    if not str(mglc["mode"]).strip():
        raise ConfigError("mglc.mode must not be empty (e.g. videoEdit)")
    if str(mglc["resolution"]) not in ("480p", "720p", "1080p", "4K"):
        raise ConfigError("mglc.resolution must be 480p | 720p | 1080p | 4K")
    if not 1 <= int(mglc["num"]) <= 4:
        raise ConfigError("mglc.num must be in 1..4 (the platform's own limit)")
    if not isinstance(mglc["keep_original_sound"], bool):
        raise ConfigError("mglc.keep_original_sound must be true or false")
    if int(mglc.get("session_id", 0)) < 0:
        raise ConfigError("mglc.session_id must be >= 0 (0 means personal quota)")
    if int(mglc["duration_s"]) < 0:
        raise ConfigError("mglc.duration_s must be >= 0 (0 means 'derive from the input')")
    if ":" not in str(mglc["aspect_ratio"]):
        raise ConfigError(f"mglc.aspect_ratio {mglc['aspect_ratio']!r} is not W:H (e.g. 21:9)")
    scale = str(mglc["scale"]).strip().lower()
    if scale not in ("auto", "full", "half", "quarter"):
        left, sep, right = scale.partition("x")
        if not (sep and left.isdigit() and right.isdigit()
                and int(left) >= 2 and int(right) >= 2
                and int(left) % 2 == 0 and int(right) % 2 == 0):
            raise ConfigError("mglc.scale must be auto | full | half | quarter | WxH "
                              "(even sides, e.g. 4200x1800)")
    if not str(mglc["prompt_template"]).strip():
        raise ConfigError("mglc.prompt_template must not be empty")
    if not str(mglc["model_list_url"]).startswith("http"):
        raise ConfigError("mglc.model_list_url must be an http(s) URL")
    if float(mglc["models_max_age_h"]) < 0:
        raise ConfigError("mglc.models_max_age_h must be >= 0")
    for key in ("poll_interval_s", "poll_timeout_s", "review_timeout_s"):
        if float(mglc[key]) <= 0:
            raise ConfigError(f"mglc.{key} must be > 0")
    if not 0 <= float(mglc["upload_crf"]) <= 51:
        raise ConfigError("mglc.upload_crf must be in 0..51")
    if int(mglc.get("image_model_id", 44)) < 1:
        raise ConfigError("mglc.image_model_id must be >= 1")
    if ":" not in str(mglc.get("image_ratio", "16:9")):
        raise ConfigError("mglc.image_ratio must be W:H (e.g. 16:9)")
    if str(mglc.get("image_resolution", "4K")) not in ("1K", "2K", "4K"):
        raise ConfigError("mglc.image_resolution must be 1K | 2K | 4K")
    if not 1 <= int(mglc.get("image_num", 4)) <= 4:
        raise ConfigError("mglc.image_num must be in 1..4")
