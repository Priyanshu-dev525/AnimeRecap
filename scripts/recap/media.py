"""ffmpeg / ffprobe helpers."""
from __future__ import annotations

import json
import subprocess
import tempfile
from fractions import Fraction
from pathlib import Path

from .common import ToolError, log, parse_time

FFMPEG_HINT = "Install ffmpeg (it includes ffprobe), make sure both are on PATH, then run check_setup."

TEXT_SUB_CODECS = {
    "subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text", "microdvd",
    "subviewer", "subviewer1", "realtext", "sami", "stl", "jacosub", "mpl2", "pjs", "vplayer",
}


def _tail(data: bytes, lines: int = 6) -> str:
    text = data.decode("utf-8", "replace").strip().splitlines()
    return " | ".join(text[-lines:]) or "no error output"


def run(cmd: list, cwd=None, check: bool = True) -> subprocess.CompletedProcess:
    cmd = [str(c) for c in cmd]
    try:
        proc = subprocess.run(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        raise ToolError(f"'{cmd[0]}' was not found.", hint=FFMPEG_HINT)
    if check and proc.returncode != 0:
        raise ToolError(f"{Path(cmd[0]).name} failed: {_tail(proc.stderr)}",
                        hint="Check that the input file exists and plays correctly.")
    return proc


def run_ffmpeg(args: list, duration: float = 0.0, label: str = "", cwd=None) -> None:
    """Run ffmpeg and log coarse progress (every 10%) to stderr."""
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-progress", "pipe:1", "-y", *[str(a) for a in args]]
    with tempfile.TemporaryFile() as err:
        try:
            proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=err)
        except FileNotFoundError:
            raise ToolError("'ffmpeg' was not found.", hint=FFMPEG_HINT)
        last = -10
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if label and duration > 0 and line.startswith(("out_time_us=", "out_time_ms=")):
                try:
                    seconds = int(line.split("=", 1)[1]) / 1_000_000  # both keys are microseconds
                except ValueError:
                    continue
                pct = int(min(99.0, seconds / duration * 100) // 10) * 10
                if pct >= last + 10:
                    last = pct
                    log(f"{label}: {pct}%")
        code = proc.wait()
        if code != 0:
            err.seek(0)
            where = f" during {label}" if label else ""
            raise ToolError(f"ffmpeg failed{where}: {_tail(err.read())}",
                            hint="Check the input file. Run check_setup if ffmpeg itself seems broken.")


def probe(path: Path) -> dict:
    proc = run(["ffprobe", "-v", "error", "-print_format", "json",
                "-show_format", "-show_streams", "-show_chapters", path])
    try:
        return json.loads(proc.stdout.decode("utf-8", "replace") or "{}")
    except json.JSONDecodeError:
        raise ToolError(f"ffprobe returned unreadable output for {path}")


def parse_rate(value) -> float:
    try:
        rate = Fraction(str(value))
    except (ValueError, ZeroDivisionError):
        return 0.0
    return float(rate) if rate > 0 else 0.0


def _tag(stream: dict, key: str) -> str:
    for k, v in (stream.get("tags") or {}).items():
        if k.lower() == key:
            return str(v)
    return ""


def _flag(stream: dict, key: str) -> bool:
    return bool((stream.get("disposition") or {}).get(key))


def _positive_float(*values) -> float:
    for value in values:
        try:
            number = parse_time(value)
        except (ToolError, TypeError):
            continue
        if number > 0:
            return number
    return 0.0


def video_info(path: Path) -> dict:
    data = probe(path)
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video" and not _flag(s, "attached_pic")), None)
    if video is None:
        raise ToolError(f"No video stream in {path.name}.", hint="Pass an episode video file (mkv, mp4, ...).")
    fmt = data.get("format") or {}
    duration = _positive_float(fmt.get("duration"), video.get("duration"), _tag(video, "duration"))
    if duration <= 0:
        raise ToolError(f"Could not read the duration of {path.name}.", hint="Remux the file (ffmpeg -i in -c copy out.mkv) and retry.")
    sar = video.get("sample_aspect_ratio") or "1:1"
    if sar in ("0:1", "N/A"):
        sar = "1:1"
    audio = [{
        "index": s["index"], "codec": s.get("codec_name"), "language": _tag(s, "language") or "und",
        "title": _tag(s, "title"), "channels": s.get("channels"), "default": _flag(s, "default"),
    } for s in streams if s.get("codec_type") == "audio"]
    subs = [{
        "index": s["index"], "codec": s.get("codec_name"), "language": _tag(s, "language") or "und",
        "title": _tag(s, "title"), "forced": _flag(s, "forced"), "default": _flag(s, "default"),
        "text_based": (s.get("codec_name") or "") in TEXT_SUB_CODECS,
    } for s in streams if s.get("codec_type") == "subtitle"]
    chapters = [{
        "title": _tag(c, "title"),
        "start": round(_positive_float(c.get("start_time")), 2),
        "end": round(_positive_float(c.get("end_time")), 2),
    } for c in data.get("chapters") or []]
    return {
        "path": str(path),
        "duration": duration,
        "fps": parse_rate(video.get("avg_frame_rate")) or parse_rate(video.get("r_frame_rate")) or 24.0,
        "width": video.get("width"),
        "height": video.get("height"),
        "sar": sar,
        "video_index": video["index"],
        "video_codec": video.get("codec_name"),
        "audio": audio,
        "subtitles": subs,
        "chapters": chapters,
        "size": int(fmt.get("size") or path.stat().st_size),
    }


def fit_filter(width: int, height: int, sar: str = "1:1") -> str:
    """Scale to fit inside width x height with square pixels, then pad with black (works on old ffmpeg too)."""
    parts = []
    if sar and sar != "1:1":
        parts += ["scale='trunc(iw*sar/2)*2':ih", "setsar=1"]
    parts.append(f"scale='trunc(min({width}/iw,{height}/ih)*iw/2)*2':'trunc(min({width}/iw,{height}/ih)*ih/2)*2'")
    parts.append(f"pad={width}:{height}:'(ow-iw)/2':'(oh-ih)/2':color=black")
    parts.append("setsar=1")
    return ",".join(parts)


def extract_frame(video: Path, seconds: float, out: Path, width: int, height: int,
                  sar: str = "1:1", quality: int = 3) -> Path:
    """Grab one frame (JPEG, or PNG when `out` ends in .png). Steps back a little if the time is past the end."""
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".part" + out.suffix)
    for t in (seconds, max(0.0, seconds - 0.5), max(0.0, seconds - 2.0)):
        cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-threads", "2", "-ss", f"{t:.3f}", "-i", video,
               "-frames:v", "1", "-vf", fit_filter(width, height, sar)]
        if out.suffix.lower() in (".jpg", ".jpeg"):
            cmd += ["-q:v", str(quality)]
        run(cmd + [tmp], check=False)
        if tmp.exists() and tmp.stat().st_size > 0:
            tmp.replace(out)
            return out
    raise ToolError(f"Could not extract a frame at {seconds:.2f}s from {Path(video).name}.")
