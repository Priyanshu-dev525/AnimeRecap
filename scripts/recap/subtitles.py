"""English dialogue: sidecar subtitle files, embedded text tracks, or (optionally) Whisper."""
from __future__ import annotations

import importlib.util
import os
import re
from pathlib import Path

from .common import ToolError, existing_file, log
from .media import run

SUB_EXTS = (".srt", ".ass", ".ssa", ".vtt")
ENGLISH = {"en", "eng", "english"}
OTHER_LANGS = {
    "ja", "jp", "jpn", "japanese", "es", "spa", "spanish", "pt", "por", "ptbr", "fr", "fre", "fra", "french",
    "de", "ger", "deu", "german", "it", "ita", "ru", "rus", "ar", "ara", "zh", "chi", "zho", "chs", "cht",
    "ko", "kor", "id", "ind", "ms", "may", "msa", "th", "tha", "vi", "vie", "tr", "tur", "pl", "pol",
    "nl", "dut", "nld", "hi", "hin", "sv", "swe", "fi", "fin", "no", "nor", "da", "dan", "cs", "cze", "ces",
    "hu", "hun", "ro", "rum", "ron", "el", "gre", "ell", "he", "heb", "uk", "ukr", "fil", "tl",
}
# ASS styles that hold signs, song lyrics, titles or notes rather than dialogue.
SIGN_STYLE_RE = re.compile(
    r"sign|song|lyric|karaoke|kara|romaji|kanji|insert|typeset|title|note|screen|eyecatch|preview|"
    r"(^|[^a-z])(op|ed|ts)([^a-z]|$)", re.IGNORECASE)
DIALOGUE_STYLE_RE = re.compile(r"default|main|dialog|italic|top|flashback|narrat|thought|overlap|internal", re.IGNORECASE)
KARAOKE_RE = re.compile(r"\\(k|kf|ko|K)\d")
DRAWING_RE = re.compile(r"\\p[1-9]")
POSITION_RE = re.compile(r"\\(pos|move)\(")
TIME_LINE_RE = re.compile(
    r"((?:\d+:)?\d{1,2}:\d{2}(?:[.,]\d{1,3})?)\s*-->\s*((?:\d+:)?\d{1,2}:\d{2}(?:[.,]\d{1,3})?)")


# ---------------------------------------------------------------- parsing

def _seconds(stamp: str) -> float:
    total = 0.0
    for part in stamp.replace(",", ".").split(":"):
        total = total * 60 + float(part)
    return total


def _clean(text: str) -> str:
    text = re.sub(r"\{[^}]*\}", "", text)          # ASS override blocks
    text = re.sub(r"<[^>]+>", "", text)            # <i>, <font ...>
    text = text.replace("\\N", " ").replace("\\n", " ").replace("\\h", " ")
    return re.sub(r"\s+", " ", text).strip()


def parse_srt_like(text: str) -> list[dict]:
    """SRT and WebVTT."""
    lines, times, buf = [], None, []

    def flush():
        if times and buf:
            cleaned = _clean(" ".join(buf))
            if cleaned:
                lines.append({"start": times[0], "end": times[1], "text": cleaned})

    for raw in text.splitlines():
        line = raw.strip()
        match = TIME_LINE_RE.search(line)
        if match:
            flush()
            buf = []
            try:
                times = (_seconds(match.group(1)), _seconds(match.group(2)))
            except ValueError:
                times = None
        elif not line:
            flush()
            times, buf = None, []
        elif times is not None:
            buf.append(line)
    flush()
    return lines


def _is_sign(style: str, actor: str, raw: str) -> bool:
    if KARAOKE_RE.search(raw) or DRAWING_RE.search(raw):
        return True
    if SIGN_STYLE_RE.search(style) and not DIALOGUE_STYLE_RE.search(style):
        return True
    if actor.strip().lower() in {"sign", "signs", "ts", "song", "op", "ed", "lyrics"}:
        return True
    # Positioned text in an unknown style is almost always an on-screen sign.
    return bool(POSITION_RE.search(raw)) and not DIALOGUE_STYLE_RE.search(style)


def parse_ass(text: str) -> list[dict]:
    lines, fields, in_events = [], None, False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            in_events = line.lower() == "[events]"
            continue
        if not in_events:
            continue
        lower = line.lower()
        if lower.startswith("format:"):
            fields = [f.strip().lower() for f in line.split(":", 1)[1].split(",")]
        elif lower.startswith("dialogue:") and fields:
            values = line.split(":", 1)[1].split(",", len(fields) - 1)
            if len(values) < len(fields):
                continue
            row = dict(zip(fields, values))
            body = row.get("text", "")
            if _is_sign(row.get("style", "").strip(), row.get("name", ""), body):
                continue
            cleaned = _clean(body)
            if not cleaned:
                continue
            try:
                lines.append({"start": _seconds(row["start"].strip()), "end": _seconds(row["end"].strip()), "text": cleaned})
            except (KeyError, ValueError):
                continue
    return lines


def tidy(lines: list[dict]) -> list[dict]:
    """Sort, drop song lyrics, merge duplicates (ASS often layers the same line)."""
    keep = [l for l in lines if l["text"] and l["end"] >= l["start"] and not l["text"].startswith(("♪", "♫", "～♪"))]
    keep.sort(key=lambda l: (l["start"], l["end"]))
    out: list[dict] = []
    for line in keep:
        if out and line["text"] == out[-1]["text"] and line["start"] <= out[-1]["end"] + 0.5:
            out[-1]["end"] = max(out[-1]["end"], round(line["end"], 3))
            continue
        out.append({"start": round(line["start"], 3), "end": round(line["end"], 3), "text": line["text"]})
    return out


def read_text_file(path: Path) -> str:
    data = path.read_bytes()
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", "replace")
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def parse_subtitle_file(path: Path) -> list[dict]:
    text = read_text_file(path)
    if path.suffix.lower() in (".ass", ".ssa") or "[Events]" in text:
        return tidy(parse_ass(text))
    return tidy(parse_srt_like(text))


# ---------------------------------------------------------------- finding tracks

def find_external_subtitles(video: Path) -> list[Path]:
    """Sidecar files named like the video (ep01.srt, ep01.en.ass, ...), best first."""
    stem = video.stem.lower()
    found = []
    try:
        siblings = list(video.parent.iterdir())
    except OSError:
        return []
    for f in siblings:
        if f.suffix.lower() not in SUB_EXTS or not f.name.lower().startswith(stem + "."):
            continue
        tags = {t for t in re.split(r"[^a-z]+", f.name[len(stem):].lower()) if t}
        tags.discard(f.suffix.lower().lstrip("."))
        if tags & ENGLISH:
            score = 10
        elif tags & OTHER_LANGS:
            continue
        else:
            score = 5  # no language tag: assume English
        if tags & {"forced", "signs", "sign", "songs", "song"}:
            score -= 8
        if f.suffix.lower() in (".ass", ".ssa"):
            score += 1
        found.append((score, f.name, f))
    return [f for _, _, f in sorted(found, key=lambda x: (-x[0], x[1]))]


def rank_subtitle_streams(info: dict) -> list[dict]:
    """Embedded English text tracks, best first (full dialogue beats signs/songs)."""
    ranked = []
    for s in info.get("subtitles", []):
        if not s["text_based"]:
            continue
        lang = (s["language"] or "").lower()
        title = (s["title"] or "").lower()
        english = lang in ENGLISH or "english" in title or title in ENGLISH
        if not english and lang not in ("", "und"):
            continue
        score = 10 if english else 2
        if s["forced"] or re.search(r"sign|song|forced|karaoke|lyric", title):
            score -= 8
        if re.search(r"full|dialog", title):
            score += 3
        if s["default"]:
            score += 1
        if s["codec"] in ("ass", "ssa"):
            score += 1
        ranked.append((score, s))
    return [s for _, s in sorted(ranked, key=lambda x: -x[0])]


def extract_subtitle_stream(video: Path, stream: dict, workdir: Path) -> Path:
    workdir.mkdir(parents=True, exist_ok=True)
    if stream["codec"] in ("ass", "ssa"):
        out, codec = workdir / f"sub_{stream['index']}.ass", "copy"
    else:
        out, codec = workdir / f"sub_{stream['index']}.srt", "srt"
    run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", video, "-map", f"0:{stream['index']}", "-c:s", codec, out])
    return out


def whisper_engine() -> str | None:
    if importlib.util.find_spec("faster_whisper"):
        return "faster-whisper"
    if importlib.util.find_spec("whisper"):
        return "openai-whisper"
    return None


def transcribe(video: Path, info: dict, workdir: Path, required: bool = False) -> list[dict]:
    """Whisper fallback. Non-English audio is translated to English."""
    engine = whisper_engine()
    if engine is None:
        if required:
            raise ToolError("Whisper is not installed.",
                            hint="pip install faster-whisper (or openai-whisper), or put an English .srt next to the video.")
        return []
    tracks = info.get("audio") or []
    if not tracks:
        return []
    english = next((a for a in tracks if a["language"].lower() in ENGLISH), None)
    track = english or next((a for a in tracks if a["default"]), tracks[0])
    task = "transcribe" if english else "translate"
    model_name = os.environ.get("WHISPER_MODEL", "small")
    workdir.mkdir(parents=True, exist_ok=True)
    wav = workdir / f"audio_{track['index']}_16k.wav"
    if not wav.exists():
        run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", video, "-map", f"0:{track['index']}",
             "-vn", "-ac", "1", "-ar", "16000", wav])
    log(f"transcribing {video.name} with {engine} ({model_name}, task={task}); this can take several minutes")
    if engine == "faster-whisper":
        from faster_whisper import WhisperModel  # type: ignore
        model = WhisperModel(model_name, device="auto", compute_type="auto")
        segments, _ = model.transcribe(str(wav), task=task, vad_filter=True)
        lines = [{"start": s.start, "end": s.end, "text": s.text.strip()} for s in segments]
    else:
        import whisper  # type: ignore
        result = whisper.load_model(model_name).transcribe(str(wav), task=task, fp16=False)
        lines = [{"start": s["start"], "end": s["end"], "text": s["text"].strip()} for s in result["segments"]]
    return tidy(lines)


def load_dialogue(video: Path, info: dict, workdir: Path, explicit=None, whisper="auto") -> tuple[list[dict], str]:
    """Return (lines, description of the source). Prefers a given file, then sidecars, then embedded tracks."""
    if explicit:
        path = existing_file(explicit, "subtitle file")
        return parse_subtitle_file(path), f"file {path.name}"
    for path in find_external_subtitles(video):
        lines = parse_subtitle_file(path)
        if len(lines) >= 5:
            return lines, f"sidecar file {path.name}"
    for stream in rank_subtitle_streams(info):
        try:
            lines = parse_subtitle_file(extract_subtitle_stream(video, stream, workdir))
        except ToolError as exc:
            log(f"could not read subtitle track #{stream['index']}: {exc}")
            continue
        if len(lines) >= 5:
            title = f", {stream['title']}" if stream["title"] else ""
            return lines, f"embedded track #{stream['index']} ({stream['language']}{title})"
    if str(whisper).lower() not in ("false", "off", "no", "0"):
        lines = transcribe(video, info, workdir, required=whisper is True or str(whisper).lower() == "true")
        if lines:
            return lines, f"whisper ({whisper_engine()})"
    return [], "none (no English subtitles; add an .srt next to the video or install faster-whisper)"
