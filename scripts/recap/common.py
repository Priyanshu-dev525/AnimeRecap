"""Shared helpers: errors, logging, JSON files, paths and project storage."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
SKILL_DIR = PKG_DIR.parent.parent
REFERENCES_DIR = SKILL_DIR / "references"

WORDS_PER_MINUTE = 190
PAUSE_SEC = 0.35   # silence after each beat
TAIL_SEC = 1.0     # silence after the last beat
CLOSING_LINE = (
    "But that's where this video ends. Remember to like and subscribe to the channel. "
    "Thanks for watching and I'll see you next time."
)


class ToolError(Exception):
    """A failure the host AI can act on; `hint` says what to do next."""

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.hint = hint


def log(message: str) -> None:
    """Progress goes to stderr so stdout stays clean JSON."""
    print(f"[animerecap] {message}", file=sys.stderr, flush=True)


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def read_json(path: Path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def write_json(path: Path, data) -> None:
    """Write atomically so an interrupted command never leaves a broken file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def short_hash(*parts) -> str:
    h = hashlib.sha1()
    for part in parts:
        h.update(str(part).encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:10]


def slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return slug[:max_len].strip("-") or "recap"


def fmt_time(seconds: float) -> str:
    """MM:SS (or H:MM:SS), rounded down like YouTube timestamps."""
    total = int(max(0.0, seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def parse_time(value) -> float:
    """Seconds as a number or string, or 'MM:SS' / 'H:MM:SS'."""
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    try:
        return float(text)
    except ValueError:
        pass
    total = 0.0
    try:
        for part in text.split(":"):
            total = total * 60 + float(part)
    except ValueError:
        raise ToolError(f"Cannot read the time '{value}'.", hint="Use seconds (95.5) or MM:SS ('01:35').")
    return total


def word_count(text: str) -> int:
    return len(text.split())


def speech_seconds(words: int) -> float:
    return words / WORDS_PER_MINUTE * 60.0


def segment_durations(spoken: list[float]) -> list[float]:
    """Length of each beat's video segment: its narration plus the pause after it."""
    out = [d + PAUSE_SEC for d in spoken]
    if out:
        out[-1] += TAIL_SEC - PAUSE_SEC
    return out


def to_path(value) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(value).strip()))).resolve()


def existing_file(value, what: str = "file") -> Path:
    if not value:
        raise ToolError(f"Missing {what} path.")
    path = to_path(value)
    if not path.is_file():
        raise ToolError(f"{what.capitalize()} not found: {path}", hint="Check the path; quote paths that contain spaces.")
    return path


def compact_ids(ids) -> str:
    """[3, 4, 5, 9] -> '3-5, 9'."""
    nums = sorted({int(i) for i in ids})
    if not nums:
        return ""
    parts = []
    start = prev = nums[0]
    for n in nums[1:] + [None]:
        if n is not None and n == prev + 1:
            prev = n
            continue
        parts.append(f"{start}-{prev}" if prev > start else str(start))
        if n is not None:
            start = prev = n
    return ", ".join(parts)


class Budget:
    """Long tools stop cleanly before the host app's command timeout (default 9 minutes).

    Everything finished is cached, so running the same command again continues.
    """

    def __init__(self):
        try:
            self.limit = float(os.environ.get("RECAP_TIME_BUDGET", "540"))
        except ValueError:
            self.limit = 540.0
        self.start = time.monotonic()

    def left(self) -> float:
        return self.limit - (time.monotonic() - self.start)

    def expired(self, reserve: float = 0.0) -> bool:
        return self.left() < reserve


# ---------------------------------------------------------------- projects

def candidate_homes() -> list[Path]:
    env = os.environ.get("RECAP_HOME")
    if env:
        return [to_path(env)]
    return [Path.home() / "animerecap_projects", Path.cwd() / "animerecap_projects"]


_HOME: Path | None = None


def recap_home() -> Path:
    """The first writable projects folder (sandboxed apps may only allow the working directory)."""
    global _HOME
    if _HOME is not None:
        return _HOME
    errors = []
    for base in candidate_homes():
        try:
            base.mkdir(parents=True, exist_ok=True)
            probe = base / ".write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            _HOME = base
            return base
        except OSError as exc:
            errors.append(f"{base}: {exc}")
    raise ToolError("No writable folder for recap projects.", hint="Set RECAP_HOME to a writable folder. " + "; ".join(errors))


def find_project_dir(project_id: str) -> Path | None:
    for base in candidate_homes():
        folder = base / project_id
        if (folder / "project.json").is_file():
            return folder
    return None


class Project:
    """One project folder: project.json, scenes.json, script.json, voice.json, output/."""

    def __init__(self, project_id):
        pid = str(project_id or "").strip()
        if not pid:
            raise ToolError("project_id is required.",
                            hint="Use the project_id returned by analyze_video, or run list_projects.")
        folder = find_project_dir(pid)
        if folder is None:
            raise ToolError(f"Project '{pid}' was not found.",
                            hint="Run analyze_video first, or list_projects. Keep RECAP_HOME the same for every command.")
        self.id = pid
        self.dir = folder
        self._meta = None
        self._scenes = None

    @classmethod
    def from_args(cls, args: dict) -> "Project":
        return cls(args.get("project_id"))

    def path(self, *parts) -> Path:
        return self.dir.joinpath(*parts)

    @property
    def meta(self) -> dict:
        if self._meta is None:
            self._meta = read_json(self.path("project.json"), {}) or {}
        return self._meta

    def save_meta(self) -> None:
        write_json(self.path("project.json"), self.meta)

    @property
    def scenes(self) -> list:
        if self._scenes is None:
            self._scenes = read_json(self.path("scenes.json"), None)
            if self._scenes is None or not self.meta.get("analyzed"):
                raise ToolError("This project is not fully analyzed yet.",
                                hint="Run the same analyze_video command again; it continues where it stopped.")
        return self._scenes

    def save_scenes(self) -> None:
        write_json(self.path("scenes.json"), self.scenes)

    def get_scene(self, scene_id) -> dict | None:
        try:
            index = int(scene_id)
        except (TypeError, ValueError):
            return None
        if 1 <= index <= len(self.scenes):
            return self.scenes[index - 1]
        return None

    def scene(self, scene_id) -> dict:
        found = self.get_scene(scene_id)
        if found is None:
            raise ToolError(f"Scene {scene_id} does not exist.", hint=f"Scene ids run from 1 to {len(self.scenes)}.")
        return found

    def video(self, episode) -> dict:
        videos = self.meta.get("videos") or []
        try:
            return videos[int(episode) - 1]
        except (IndexError, TypeError, ValueError):
            raise ToolError(f"Episode {episode} does not exist.", hint=f"Episodes run from 1 to {len(videos)}.")

    def in_range(self, scene: dict) -> bool:
        rng = self.video(scene["episode"]).get("content_range")
        if not rng:
            return True
        middle = (scene["start"] + scene["end"]) / 2
        return rng[0] <= middle <= rng[1]

    def usable(self, scene: dict) -> bool:
        """Credits and footage outside the content range are never shown."""
        return not scene.get("credits") and self.in_range(scene)

    def label_time(self, scene: dict) -> str:
        stamp = fmt_time(scene["start"])
        return f"E{scene['episode']} {stamp}" if len(self.meta.get("videos") or []) > 1 else stamp

    def keyframe(self, scene: dict) -> Path:
        return self.path("keyframes", f"{scene['id']:05d}.jpg")

    def load_script(self) -> dict:
        data = read_json(self.path("script.json"), None) or {}
        data.setdefault("beats", [])
        return data

    def save_script(self, data: dict) -> None:
        write_json(self.path("script.json"), data)

    def load_voice(self) -> dict:
        return read_json(self.path("voice.json"), None) or {}
