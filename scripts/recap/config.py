"""User settings: Fish Audio key and cloned voice, channel logo, background music, name cards.

Stored in config.json in the skill folder (or the file RECAP_CONFIG names). Environment
variables override the file, and tool arguments override both.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .common import SKILL_DIR, ToolError, to_path, write_json

ASSETS_DIR = SKILL_DIR / "assets"
AUDIO_EXTS = (".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus")
IMAGE_EXTS = (".png", ".webp", ".jpg", ".jpeg")
OFF = ("none", "off", "false", "no")

DEFAULTS: dict = {
    "fish_api_key": "",
    "fish_voice_id": "",        # reference_id of your cloned voice on fish.audio
    "fish_model": "s1",
    "edge_voice": "en-US-GuyNeural",
    "logo": "",                 # empty: assets/logo.png (or .webp/.jpg) when it exists
    "logo_size": 0.12,          # logo width as a fraction of the video width
    "logo_opacity": 0.9,
    "music": "",                # a file or folder; empty: assets/music/ when it has audio files
    "music_volume": 0.12,       # music loudness relative to the voice (1.0 = as loud as the voice)
    "music_shuffle": False,
    "character_names": True,    # bold white name at the bottom right when a character is introduced
    "name_seconds": 3.0,
}
ENV_VARS = {"fish_api_key": "FISH_API_KEY", "fish_voice_id": "FISH_VOICE_ID", "fish_model": "FISH_MODEL",
            "edge_voice": "EDGE_VOICE", "logo": "RECAP_LOGO", "music": "RECAP_MUSIC"}
RANGES = {"logo_size": (0.03, 0.4), "logo_opacity": (0.1, 1.0), "music_volume": (0.0, 1.0), "name_seconds": (1.0, 8.0)}


def config_path() -> Path:
    env = os.environ.get("RECAP_CONFIG")
    return to_path(env) if env else SKILL_DIR / "config.json"


def _stored() -> dict:
    path = config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise ToolError(f"{path} is not valid JSON: {exc}", hint="Fix the file, or delete it and run configure again.")
    if not isinstance(data, dict):
        raise ToolError(f"{path} must hold a JSON object.")
    return data


def coerce(key: str, value):
    """Check one setting's type and range."""
    default = DEFAULTS[key]
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("true", "yes", "on", "1"):
            return True
        if text in ("false", "no", "off", "0"):
            return False
        raise ToolError(f"{key} must be true or false.")
    if isinstance(default, float):
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ToolError(f"{key} must be a number.")
        low, high = RANGES[key]
        if not low <= number <= high:
            raise ToolError(f"{key} must be between {low} and {high}.")
        return number
    return str(value).strip()


def load_settings(overrides: dict | None = None) -> dict:
    settings = dict(DEFAULTS)
    for key, value in _stored().items():
        if key in DEFAULTS and value is not None:
            settings[key] = coerce(key, value)
    for key, env in ENV_VARS.items():
        if os.environ.get(env):
            settings[key] = coerce(key, os.environ[env])
    for key, value in (overrides or {}).items():
        if key in DEFAULTS and value is not None:
            settings[key] = coerce(key, value)
    return settings


def user_path(value: str) -> Path:
    """Absolute paths as given; relative paths are relative to the skill folder."""
    path = Path(os.path.expandvars(os.path.expanduser(value)))
    return path if path.is_absolute() else (SKILL_DIR / path).resolve()


def find_logo(settings: dict) -> tuple[Path | None, str]:
    value = (settings.get("logo") or "").strip()
    if value.lower() in OFF:
        return None, "off"
    if value:
        path = user_path(value)
        return (path, str(path)) if path.is_file() else (None, f"missing: {path}")
    for ext in IMAGE_EXTS:
        path = ASSETS_DIR / f"logo{ext}"
        if path.is_file():
            return path, str(path)
    return None, "none (put logo.png in assets/, or run configure with logo)"


def find_music(settings: dict) -> tuple[list[Path], str]:
    value = (settings.get("music") or "").strip()
    if value.lower() in OFF:
        return [], "off"
    base = user_path(value) if value else ASSETS_DIR / "music"
    if base.is_file():
        tracks = [base] if base.suffix.lower() in AUDIO_EXTS else []
    elif base.is_dir():
        tracks = sorted(p for p in base.iterdir() if p.is_file() and p.suffix.lower() in AUDIO_EXTS)
    else:
        tracks = []
    if tracks:
        return tracks, f"{len(tracks)} track(s): {base}"
    if value:
        return [], f"missing or no audio files: {base}"
    return [], "none (put music files in assets/music/, or run configure with music)"


def _mask(secret: str) -> str:
    if not secret:
        return "missing"
    return f"set (ends in ...{secret[-4:]})" if len(secret) > 8 else "set"


def describe(settings: dict) -> dict:
    """Settings as shown to the user: the API key masked, paths checked."""
    stored = _stored()

    def source(key: str) -> str:
        if key in ENV_VARS and os.environ.get(ENV_VARS[key]):
            return "environment"
        return "config.json" if key in stored else "default"

    fish = bool(settings["fish_api_key"] and settings["fish_voice_id"])
    return {
        "config_file": str(config_path()),
        "voice": (f"fish: your cloned voice {settings['fish_voice_id']}" if fish else
                  f"edge: free test voice {settings['edge_voice']} (add fish_api_key and fish_voice_id for your cloned voice)"),
        "fish_api_key": _mask(settings["fish_api_key"]),
        "fish_voice_id": settings["fish_voice_id"] or "missing",
        "fish_model": settings["fish_model"],
        "logo": find_logo(settings)[1],
        "logo_size": settings["logo_size"],
        "logo_opacity": settings["logo_opacity"],
        "music": find_music(settings)[1],
        "music_volume": settings["music_volume"],
        "music_shuffle": settings["music_shuffle"],
        "character_names": settings["character_names"],
        "name_seconds": settings["name_seconds"],
        "set_by": {k: source(k) for k in DEFAULTS if source(k) != "default"},
    }


def _check_path(key: str, value: str) -> str:
    if value.lower() in OFF:
        return "none"
    path = user_path(value)
    if key == "logo":
        from PIL import Image
        try:
            with Image.open(path) as im:
                im.load()
        except (OSError, ValueError):
            raise ToolError(f"The logo {path} is not an image file Pillow can open.", hint="Use a PNG (transparent background works best).")
    elif not find_music({"music": str(path)})[0]:
        raise ToolError(f"No music files found at {path}.", hint="Give an audio file or a folder of " + ", ".join(AUDIO_EXTS) + " files.")
    return str(path)


def configure(args: dict) -> dict:
    """Save settings to config.json. A null or empty value resets a setting to its default."""
    stored = _stored()
    changed = []
    for key, value in args.items():
        if key not in DEFAULTS:
            raise ToolError(f"Unknown setting '{key}'.", hint="Settings: " + ", ".join(DEFAULTS))
        if value is None or (isinstance(value, str) and not value.strip()):
            stored.pop(key, None)
        else:
            value = coerce(key, value)
            stored[key] = _check_path(key, value) if key in ("logo", "music") else value
        changed.append(key)
    path = config_path()
    if changed:
        try:
            write_json(path, stored)
            os.chmod(path, 0o600)  # it may hold an API key
        except OSError as exc:
            raise ToolError(f"Cannot write {path}: {exc}",
                            hint="Set environment variables instead (FISH_API_KEY, FISH_VOICE_ID), "
                                 "or point RECAP_CONFIG to a writable file.")
    result = {"changed": changed, "settings": describe(load_settings())}
    overridden = [k for k in changed if k in ENV_VARS and os.environ.get(ENV_VARS[k])]
    if overridden:
        result["note"] = f"Environment variables override: {', '.join(overridden)}."
    return result
