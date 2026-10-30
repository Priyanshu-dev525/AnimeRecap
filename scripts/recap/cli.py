"""Command line: python scripts/recap.py <tool> '<json>' | <file.json> | key=value ...

Every tool prints one JSON object. Tools that make a picture also print
`IMAGE: <path>` lines; the host AI opens those files and looks at them.
"""
from __future__ import annotations

import difflib
import json
import os
import sys
import traceback
from pathlib import Path

from .common import ToolError

USAGE = """animerecap - python scripts/recap.py <tool> '<json>'

The argument is one JSON object, a path to a .json file holding it, key=value pairs
(value may be JSON or @file.json), or - to read JSON from stdin.
Tools print JSON; 'IMAGE: <path>' lines are pictures to open and look at.
"""

# (name, module, function, arguments, description)
TOOLS = [
    ("check_setup", "engine", "check_setup", "",
     "Check ffmpeg, Python packages, your settings (voice, logo, music), fonts and the projects folder."),
    ("configure", "config", "configure",
     "fish_api_key?, fish_voice_id?, fish_model?, edge_voice?, logo?, logo_size?, logo_opacity?, music?, "
     "music_volume?, music_shuffle?, character_names?, name_seconds?",
     "Save your Fish Audio key + cloned voice, channel logo, background music and name-card options."),
    ("inspect_video", "engine", "inspect_video", "path",
     "Duration, audio tracks, subtitle tracks and chapters of one video (ffprobe)."),
    ("suggest_length", "engine", "suggest_length", "episodes, paths?, requested_minutes?, project_id?",
     "Target length: 22*sqrt(episodes) min, max 90, never above 95% of usable footage; 190 words/min."),
    ("analyze_video", "engine", "analyze_video", "paths[], subtitles?[], whisper?=auto, force?",
     "Scene detection, one keyframe per scene, English dialogue. Returns project_id and scene count."),
    ("get_contact_sheet", "engine", "get_contact_sheet", "project_id, first_scene=1, count=20",
     "IMAGE: keyframe grid with scene id + time on each tile, plus each scene's dialogue."),
    ("get_scene_detail", "engine", "get_scene_detail", "project_id, scene_id, at=0.5, grid=false",
     "IMAGE: one large frame of a scene (at = 0-1 position in the shot)."),
    ("save_descriptions", "engine", "save_descriptions",
     "project_id, descriptions[{id, description, importance 1-5, credits?}]",
     "Save what scenes show. credits=true marks OP/ED/title/next-episode cards (never used)."),
    ("set_content_range", "engine", "set_content_range", "project_id, start_sec, end_sec, episode=1, clear?",
     "Use only footage between these times of an episode (cuts recaps and previews)."),
    ("save_notes", "engine", "save_notes", "project_id, notes?, characters?[], append=false",
     "Save story notes (names, world rules, plot by scene range) and the character names for name cards."),
    ("get_notes", "engine", "get_notes", "project_id",
     "Notes plus a progress summary with the next step (use it to resume)."),
    ("get_scenes_text", "engine", "get_scenes_text", "project_id, first_scene=1, count=40",
     "Scenes as text: time, importance, description, dialogue."),
    ("save_script", "script", "save_script",
     "project_id, beats[{text, scene_ids[] | scene_range[a,b], introduce?[]}], append=false, target_minutes?",
     "Save narration beats; returns words, estimated minutes, progress and warnings."),
    ("update_beats", "script", "update_beats",
     "project_id, updates[{beat, text?, scene_ids? | scene_range?, introduce?, delete?}]",
     "Fix single beats (wrong clips, wording) without resending the whole script."),
    ("generate_voice", "voice", "generate_voice", "project_id, provider=fish|edge, voice_id?, workers=3",
     "One mp3 per beat (cached by text), stores each duration."),
    ("render_video", "render", "render_video", "project_id, music?, music_volume?, logo?, character_names?",
     "1920x1080 MP4: clips per beat, original audio muted, narration + music, logo, name cards, no subtitles."),
    ("get_preview_sheet", "render", "get_preview_sheet", "project_id, first_beat=1, count=8",
     "IMAGE: what each beat shows, to verify and fix scene_ids."),
    ("get_timeline", "script", "get_timeline", "project_id",
     "Beats with MM:SS timestamps (real after render) and a markdown table."),
    ("get_thumbnail_candidates", "thumbnail", "get_thumbnail_candidates", "project_id, count=12",
     "IMAGE: best frames for the thumbnail, plus the style reference."),
    ("make_thumbnail", "thumbnail", "make_thumbnail",
     "project_id, scene_id, elements[], zoom=1, focus=[x,y], at=0.5, enhance=true, grid=false",
     "IMAGE: 1280x720 thumbnail from a real frame: arrow, label, badge, bubble (max 4)."),
    ("save_publish_info", "script", "save_publish_info", "project_id, title, description, hashtags[]",
     "Save title, description and hashtags to output/publish.txt."),
    ("list_projects", "engine", "list_projects", "",
     "Existing projects and their progress."),
]

DETAILS = {
    "configure": """Saves to config.json in the skill folder (git-ignored). No arguments: show the current settings.
  fish_api_key, fish_voice_id    your Fish Audio API key and cloned voice id (reference_id)
  fish_model, edge_voice         TTS model header (default s1); free Edge test voice (default en-US-GuyNeural)
  logo                           channel logo image shown at the bottom right ("none" to turn off)
  logo_size, logo_opacity        width as a fraction of the video (0.12); 0.1-1 (0.9)
  music, music_volume            background music file or folder ("none" to turn off); 0-1 relative to the voice (0.12)
  music_shuffle                  shuffle a folder of tracks (false)
  character_names, name_seconds  bold white name at the bottom right when a character is introduced (true); seconds (3)
A null or empty value resets a setting. Environment variables (FISH_API_KEY, FISH_VOICE_ID, ...) override the file.""",
    "save_script": """beats: [{"text": "60-100 words", "scene_ids": [12, 13, 15]}, {"text": "...", "scene_range": [16, 22]}]
scene_ids are shown in the order given (the order the words mention things), about one per 3-4 spoken seconds.
introduce: names first introduced in this beat, e.g. ["Ren"] or [{"name": "Ren", "scene": 14}] (card times to that clip).
Names saved with save_notes characters get a card automatically at their first mention.
First call: append=false (optionally target_minutes). Later chunks: append=true.""",
    "render_video": """Uses your settings: logo (bottom right), music (mixed under the narration), name cards (bottom right,
above the logo). Override one render with music / music_volume / logo / character_names, e.g. {"music": "none"}.
The video never contains subtitles or captions.""",
    "update_beats": """updates: [{"beat": 7, "scene_ids": [40, 41, 44]}, {"beat": 9, "text": "new text"}, {"beat": 12, "delete": true}]""",
    "make_thumbnail": """Coordinates are [x, y] fractions of the 1280x720 result (0,0 = top-left). grid=true overlays a guide.
elements (max 4):
  {"type": "arrow", "from": [0.15, 0.80], "to": [0.42, 0.55], "color": "yellow", "width": 0.032}
  {"type": "label", "text": "SSS SKILL", "at": [0.25, 0.15], "size": 0.11, "color": "white", "bg": "#E3161B" | "none", "angle": -4}
  {"type": "badge", "text": "+99", "at": [0.85, 0.20], "size": 0.14}
  {"type": "bubble", "text": "Short line!", "at": [0.75, 0.2], "tail": [0.62, 0.38], "size": 0.055}
zoom (1-3) crops toward focus [x, y]; at (0-1) picks the moment inside the scene.""",
    "save_descriptions": """descriptions: [{"id": 21, "description": "Ren blocks the wolf with a frying pan.", "importance": 4},
              {"id": 22, "description": "Opening credits.", "importance": 1, "credits": true}]""",
    "analyze_video": """paths: episode files in episode order. subtitles: optional subtitle file per episode.
whisper: auto (use it only if no English subtitles and it is installed) | true | false. force: start over.""",
}


def _load_json_file(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ToolError(f"{path} is not valid JSON: {exc}")


def _parse_value(value: str):
    if value.startswith("@"):
        return _load_json_file(Path(os.path.expanduser(value[1:])))
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _as_object(value, source: str) -> dict:
    if not isinstance(value, dict):
        raise ToolError(f"The {source} must be a JSON object ({{...}}), not {type(value).__name__}.",
                        hint='Wrap it, for example {"project_id": "...", "beats": [...]}.')
    return value


def parse_params(argv: list[str]) -> dict:
    params: dict = {}
    loose_files: list[str] = []
    for arg in argv:
        text = arg.strip()
        if text == "-":
            params.update(_as_object(json.loads(sys.stdin.read() or "{}"), "stdin input"))
        elif text.startswith("{"):
            try:
                params.update(_as_object(json.loads(text), "argument"))
            except json.JSONDecodeError as exc:
                raise ToolError(f"The argument is not valid JSON: {exc}",
                                hint="Write the JSON to a file and pass the file path instead (avoids shell quoting problems).")
        elif Path(os.path.expanduser(text)).is_file():
            path = Path(os.path.expanduser(text))
            if path.suffix.lower() == ".json":
                params.update(_as_object(_load_json_file(path), f"file {path.name}"))
            else:
                loose_files.append(str(path))
        elif "=" in text:
            key, value = text.split("=", 1)
            params[key.strip()] = _parse_value(value.strip())
        else:
            raise ToolError(f"Cannot read the argument: {arg[:80]}",
                            hint="Pass one JSON object, a path to a .json file, or key=value pairs.")
    if loose_files:  # convenience: `inspect_video ep1.mkv`, `analyze_video ep1.mkv ep2.mkv`
        params.setdefault("path", loose_files[0])
        params.setdefault("paths", loose_files)
    return params


def print_tools() -> None:
    print(USAGE)
    width = max(len(f"{name}({args})") for name, _, _, args, _ in TOOLS)
    width = min(width, 58)
    for name, _, _, args, desc in TOOLS:
        signature = f"{name}({args})"
        if len(signature) > width:
            print(signature)
            print(" " * width + "  " + desc)
        else:
            print(f"{signature:<{width}}  {desc}")
    print("\npython scripts/recap.py help <tool>  shows details and examples.")


def print_help(name: str) -> int:
    entry = next((t for t in TOOLS if t[0] == name), None)
    if entry is None:
        print(f"Unknown tool '{name}'. Run: python scripts/recap.py tools")
        return 2
    print(f"{entry[0]}({entry[3]})\n\n{entry[4]}")
    if name in DETAILS:
        print("\n" + DETAILS[name])
    return 0


def emit(result: dict, ok: bool = True) -> None:
    images = result.pop("_images", []) if isinstance(result, dict) else []
    print(json.dumps({"ok": ok, **result}, indent=1, ensure_ascii=False, default=str))
    for image in images:
        print(f"IMAGE: {Path(image).resolve()}")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    if not argv or (argv[0] in ("-h", "--help", "help") and len(argv) == 1):
        print_tools()
        return 0
    if argv[0] in ("help", "-h", "--help"):
        return print_help(argv[1])
    if argv[0] == "tools":
        print_tools()
        return 0
    name = argv[0]
    entry = next((t for t in TOOLS if t[0] == name), None)
    if entry is None:
        close = difflib.get_close_matches(name, [t[0] for t in TOOLS], n=3)
        emit({"error": f"Unknown tool '{name}'.", "hint": ("Did you mean: " + ", ".join(close)) if close
              else "Run: python scripts/recap.py tools"}, ok=False)
        return 2
    try:
        import importlib
        module = importlib.import_module(f".{entry[1]}", __package__)
        params = parse_params(argv[1:])
        result = getattr(module, entry[2])(params)
    except ToolError as exc:
        emit({"error": str(exc), "hint": exc.hint}, ok=False)
        return 1
    except ImportError as exc:
        emit({"error": f"Missing Python package: {exc}", "hint": "pip install -r requirements.txt"}, ok=False)
        return 1
    except KeyboardInterrupt:
        emit({"error": "Interrupted.", "hint": "Run the same command again; finished work is cached."}, ok=False)
        return 130
    except Exception as exc:  # unexpected: give the AI enough to report or work around it
        emit({"error": f"{type(exc).__name__}: {exc}",
              "traceback": traceback.format_exc().strip().splitlines()[-6:],
              "hint": "This looks like a bug in animerecap; check the inputs, then report it."}, ok=False)
        return 1
    emit(result if isinstance(result, dict) else {"result": result})
    return 0
