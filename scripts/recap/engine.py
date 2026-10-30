"""Analysis and scene tools: inspect, length, scene detection, sheets, descriptions, notes."""
from __future__ import annotations

import importlib.util
import math
import os
import platform
import re
import shutil
import sys
from bisect import bisect_right
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from PIL import Image, ImageDraw

from . import media, subtitles
from .common import (CLOSING_LINE, REFERENCES_DIR, WORDS_PER_MINUTE, Budget, Project, ToolError, candidate_homes,
                     existing_file, find_project_dir, fmt_time, log, now_iso, parse_time, read_json, recap_home,
                     short_hash, slugify, word_count, write_json)
from .sheets import draw_grid, font_path, get_font, grid_sheet

KEYFRAME_W, KEYFRAME_H = 640, 360
SCENE_THRESHOLD = 0.3
MIN_SCENE_SEC = 1.0
MAX_SCENE_SEC = 10.0
CREDIT_CHAPTER_RE = re.compile(r"\b(op|opening|ed|ending|credits?|preview|next|intro|outro)\b", re.IGNORECASE)


def length_formula(episodes: int, usable_seconds: float | None) -> tuple[float, float | None, float]:
    """(formula minutes, footage cap minutes, suggested minutes): 22*sqrt(episodes), max 90, <= 95% of usable footage."""
    formula = min(22.0 * math.sqrt(max(1, episodes)), 90.0)
    cap = 0.95 * usable_seconds / 60.0 if usable_seconds else None
    return formula, cap, min(formula, cap) if cap else formula


def usable_seconds(proj: Project) -> float:
    return sum(s["end"] - s["start"] for s in proj.scenes if proj.usable(s))


def target_minutes(proj: Project, script: dict | None = None) -> float:
    if script and script.get("target_minutes"):
        return float(script["target_minutes"])
    if proj.meta.get("target_minutes"):
        return float(proj.meta["target_minutes"])
    return round(length_formula(len(proj.meta.get("videos") or []), usable_seconds(proj))[2], 1)


# ---------------------------------------------------------------- setup / inspect / length

def check_setup(args: dict) -> dict:
    checks: dict = {"python": {"ok": sys.version_info >= (3, 10), "version": platform.python_version()}}
    for binary in ("ffmpeg", "ffprobe"):
        path = shutil.which(binary)
        version = None
        if path:
            out = media.run([binary, "-version"], check=False).stdout.decode("utf-8", "replace").splitlines()
            version = out[0] if out else None
        checks[binary] = {"ok": bool(path), "path": path, "version": version}
    import PIL
    checks["pillow"] = {"ok": True, "version": PIL.__version__}
    edge = importlib.util.find_spec("edge_tts") is not None
    checks["edge_tts"] = {"ok": edge, "note": None if edge else "pip install edge-tts (free test voice)"}
    key, voice = os.environ.get("FISH_API_KEY"), os.environ.get("FISH_VOICE_ID")
    checks["fish_audio"] = {
        "ok": bool(key and voice),
        "FISH_API_KEY": "set" if key else "missing",
        "FISH_VOICE_ID": voice or "missing",
        "FISH_MODEL": os.environ.get("FISH_MODEL") or "s1 (default)",
    }
    checks["edge_voice"] = os.environ.get("EDGE_VOICE") or "en-US-GuyNeural (default)"
    checks["whisper"] = {"available": subtitles.whisper_engine(), "note": "optional: only used when a video has no English subtitles"}
    checks["fonts"] = {"thumbnail": font_path(True), "labels": font_path(False)}
    try:
        checks["projects_folder"] = {"ok": True, "path": str(recap_home())}
    except ToolError as exc:
        checks["projects_folder"] = {"ok": False, "error": str(exc), "hint": exc.hint}
    sample = REFERENCES_DIR / "sample_recap.txt"
    if not sample.is_file():
        sample_state = "missing"
    elif sample.read_text(encoding="utf-8", errors="replace").lstrip().upper().startswith("PLACEHOLDER"):
        sample_state = "placeholder: paste your style reference script into references/sample_recap.txt"
    else:
        sample_state = "ok"
    checks["references"] = {
        "sample_recap.txt": sample_state,
        "thumbnail_reference.webp": "ok" if (REFERENCES_DIR / "thumbnail_reference.webp").is_file() else "missing (optional)",
    }
    ready = all(checks[k]["ok"] for k in ("python", "ffmpeg", "ffprobe", "projects_folder"))
    provider = "fish" if checks["fish_audio"]["ok"] else ("edge" if edge else None)
    result = {"ready": ready, "voice_provider": provider, "checks": checks}
    if provider is None:
        result["hint"] = "No voice available: set FISH_API_KEY and FISH_VOICE_ID, or pip install edge-tts."
    return result


def inspect_video(args: dict) -> dict:
    path = existing_file(args.get("path"), "video")
    info = media.video_info(path)
    sidecars = subtitles.find_external_subtitles(path)
    ranked = subtitles.rank_subtitle_streams(info)
    if sidecars:
        plan = f"sidecar file {sidecars[0].name}"
    elif ranked:
        title = f", {ranked[0]['title']}" if ranked[0]["title"] else ""
        plan = f"embedded track #{ranked[0]['index']} ({ranked[0]['language']}{title})"
    elif subtitles.whisper_engine():
        plan = "no English subtitles: will transcribe with Whisper (slow)"
    else:
        plan = "no English subtitles: dialogue will be empty unless you add an .srt next to the video or install faster-whisper"
    return {
        "path": str(path),
        "duration_sec": round(info["duration"], 2),
        "duration": fmt_time(info["duration"]),
        "size_mb": round(info["size"] / 1e6, 1),
        "video": {"codec": info["video_codec"], "width": info["width"], "height": info["height"], "fps": round(info["fps"], 3)},
        "audio_tracks": info["audio"],
        "subtitle_tracks": info["subtitles"],
        "external_subtitles": [str(p) for p in sidecars],
        "chapters": info["chapters"],
        "dialogue_source": plan,
    }


def _estimate_usable(info: dict) -> tuple[float, str]:
    credit = [c for c in info["chapters"] if CREDIT_CHAPTER_RE.search(c["title"] or "")]
    if credit:
        return max(0.0, info["duration"] - sum(max(0.0, c["end"] - c["start"]) for c in credit)), "chapters"
    cut = 180.0 if info["duration"] >= 15 * 60 else 0.1 * info["duration"]
    return max(0.0, info["duration"] - cut), "estimate (about 3 minutes of OP/ED per episode removed)"


def suggest_length(args: dict) -> dict:
    paths = args.get("paths") or ([args["path"]] if args.get("path") else [])
    if isinstance(paths, str):
        paths = [paths]
    episodes = int(args.get("episodes") or 0)
    usable, basis = None, None
    if args.get("project_id"):
        proj = Project.from_args(args)
        usable, basis = usable_seconds(proj), "analyzed scenes (credits and cut ranges excluded)"
        episodes = episodes or len(proj.meta.get("videos") or [])
    elif paths:
        usable, bases = 0.0, set()
        for p in paths:
            seconds, how = _estimate_usable(media.video_info(existing_file(p, "video")))
            usable += seconds
            bases.add(how)
        basis = ", ".join(sorted(bases))
        episodes = episodes or len(paths)
    elif args.get("footage_minutes"):
        usable, basis = float(args["footage_minutes"]) * 60, "given footage_minutes"
    if episodes < 1:
        raise ToolError("episodes is required (how many episodes are being recapped).",
                        hint='Example: {"episodes": 2, "paths": ["ep1.mkv", "ep2.mkv"]}')
    formula, cap, suggested = length_formula(episodes, usable)
    notes = []
    requested = args.get("requested_minutes") or args.get("minutes")
    if requested:
        target = float(requested)
        if cap and target > cap:
            notes.append(f"Requested {target:g} min is more than 95% of the usable footage; using {cap:.1f} min.")
            target = cap
    else:
        target = suggested
        if cap and cap < formula:
            notes.append(f"Capped by footage: 22*sqrt({episodes}) = {formula:.1f} min, but only {usable / 60:.1f} min is usable.")
    target = round(target, 1)
    target_words = int(round(target * WORDS_PER_MINUTE / 10) * 10)
    if args.get("project_id"):
        proj.meta["target_minutes"] = target
        proj.save_meta()
    return {
        "episodes": episodes,
        "target_minutes": target,
        "target_words": target_words,
        "approx_beats": max(1, round(target_words / 80)),
        "formula_minutes": round(formula, 1),
        "usable_footage_minutes": round(usable / 60, 1) if usable else None,
        "usable_basis": basis,
        "words_per_minute": WORDS_PER_MINUTE,
        "notes": notes,
    }


# ---------------------------------------------------------------- analysis

def build_scenes(cuts: list[float], duration: float) -> list[tuple[float, float]]:
    """Shots from cut times: merge shots under 1 s into their neighbour, split shots over 10 s."""
    bounds = [0.0] + [c for c in cuts if 0.05 < c < duration - 0.05] + [duration]
    merged: list[list[float]] = []
    for a, b in zip(bounds, bounds[1:]):
        if b - a <= 0.001:
            continue
        if merged and b - a < MIN_SCENE_SEC:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    if len(merged) > 1 and merged[0][1] - merged[0][0] < MIN_SCENE_SEC:
        merged[1][0] = merged[0][0]
        merged.pop(0)
    scenes = []
    for a, b in merged:
        parts = max(1, math.ceil((b - a) / MAX_SCENE_SEC - 1e-9))
        step = (b - a) / parts
        for k in range(parts):
            scenes.append((round(a + k * step, 3), round(b if k == parts - 1 else a + (k + 1) * step, 3)))
    return scenes


def _detect_cuts(video: Path, info: dict, work: Path, episode: int) -> tuple[list[float], bool]:
    """Scene-change times via select='gt(scene,0.3)'. Returns (cuts, from_cache)."""
    cache = work / f"cuts_ep{episode}.json"
    stat = video.stat()
    key = short_hash(video, stat.st_size, int(stat.st_mtime), SCENE_THRESHOLD)
    cached = read_json(cache)
    if cached and cached.get("key") == key:
        return cached["cuts"], True
    work.mkdir(parents=True, exist_ok=True)
    listing = work / f"cuts_ep{episode}.txt"
    listing.unlink(missing_ok=True)
    # Relative file name + cwd avoids escaping Windows paths inside the filter graph.
    vf = f"scale=320:-2:flags=fast_bilinear,select='gt(scene,{SCENE_THRESHOLD})',metadata=mode=print:file={listing.name}"
    media.run_ffmpeg(["-i", video, "-map", f"0:{info['video_index']}", "-an", "-sn", "-dn", "-vf", vf, "-f", "null", "-"],
                     duration=info["duration"], label=f"episode {episode} scene detection", cwd=work)
    text = listing.read_text(encoding="utf-8", errors="replace") if listing.exists() else ""
    cuts = sorted({round(float(t), 3) for t in re.findall(r"pts_time:([0-9.]+)", text)})
    write_json(cache, {"key": key, "cuts": cuts})
    return cuts, False


def _dialogue(video: Path, info: dict, pdir: Path, episode: int, explicit, whisper) -> tuple[list[dict], str]:
    cache = pdir / "subtitles" / f"ep{episode}.json"
    stat = video.stat()
    key = short_hash(video, stat.st_size, int(stat.st_mtime), explicit or "", whisper)
    cached = read_json(cache)
    if cached and cached.get("key") == key:
        return cached["lines"], cached["source"]
    lines, source = subtitles.load_dialogue(video, info, pdir / "work", explicit, whisper)
    write_json(cache, {"key": key, "source": source, "lines": lines})
    return lines, source


def _placeholder_frame(out: Path, text: str) -> None:
    img = Image.new("RGB", (KEYFRAME_W, KEYFRAME_H), (60, 20, 20))
    ImageDraw.Draw(img).text((20, 20), text, font=get_font(28), fill=(255, 255, 255))
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, quality=85)


def _keyframe_job(video: Path, scene: dict, out: Path, sar: str) -> None:
    try:
        media.extract_frame(video, scene["keyframe_time"], out, KEYFRAME_W, KEYFRAME_H, sar)
    except ToolError as exc:
        log(f"scene {scene['id']}: {exc}")
        _placeholder_frame(out, f"scene {scene['id']}: no frame")


def extract_keyframes(proj_dir: Path, scenes: list[dict], videos: list[dict], budget: Budget | None = None) -> bool:
    """One keyframe per scene (middle frame), in parallel. Returns False if it stopped for the time budget."""
    todo = [s for s in scenes if not (proj_dir / "keyframes" / f"{s['id']:05d}.jpg").exists()]
    if not todo:
        return True
    log(f"extracting {len(todo)} keyframes")
    finished = True
    with ThreadPoolExecutor(max_workers=max(2, min(8, os.cpu_count() or 4))) as pool:
        futures = []
        for s in todo:
            video = videos[s["episode"] - 1]
            out = proj_dir / "keyframes" / f"{s['id']:05d}.jpg"
            futures.append(pool.submit(_keyframe_job, Path(video["path"]), s, out, video["sar"]))
        for done, future in enumerate(as_completed(futures), 1):
            future.result()
            if done % 100 == 0:
                log(f"keyframes {done}/{len(todo)}")
            if budget and budget.expired():
                pool.shutdown(wait=True, cancel_futures=True)
                finished = False
                break
    return finished


def project_summary(proj: Project) -> dict:
    return {
        "project_id": proj.id,
        "project_dir": str(proj.dir),
        "scene_count": len(proj.scenes),
        "episodes": [{
            "episode": v["episode"],
            "file": Path(v["path"]).name,
            "duration": fmt_time(v["duration"]),
            "scenes": f"{v['first_scene']}-{v['last_scene']}",
            "dialogue": f"{v['dialogue_lines']} lines from {v['subtitle_source']}",
        } for v in proj.meta.get("videos", [])],
        "usable_footage_minutes": round(usable_seconds(proj) / 60, 1),
        "next": f'get_contact_sheet {{"project_id": "{proj.id}", "first_scene": 1}}',
    }


def analyze_video(args: dict) -> dict:
    raw = args.get("paths") or args.get("path")
    if isinstance(raw, str):
        raw = [raw]
    if not raw:
        raise ToolError("paths is required: the episode files in episode order.",
                        hint='Example: {"paths": ["/videos/ep01.mkv", "/videos/ep02.mkv"]}')
    paths = [existing_file(p, "video") for p in raw]
    explicit = args.get("subtitles") or []
    if isinstance(explicit, str):
        explicit = [explicit]
    whisper = args.get("whisper", "auto")
    force = bool(args.get("force"))
    pid = f"{slugify(paths[0].stem, 32)}-{short_hash(*paths)[:6]}"

    pdir = find_project_dir(pid)
    if pdir and not force and (read_json(pdir / "project.json", {}) or {}).get("analyzed"):
        return {**project_summary(Project(pid)), "reused": True,
                "note": "Already analyzed; descriptions and script are kept. Pass force=true to start over."}
    pdir = pdir or recap_home() / pid
    if force:
        for name in ("scenes.json", "script.json", "voice.json"):
            (pdir / name).unlink(missing_ok=True)
        for name in ("work", "keyframes", "subtitles", "frames", "sheets", "render", "voice"):
            shutil.rmtree(pdir / name, ignore_errors=True)
    old = read_json(pdir / "project.json", {}) or {}
    meta = {"project_id": pid, "created": old.get("created") or now_iso(), "analyzed": False, "videos": []}
    write_json(pdir / "project.json", meta)

    budget = Budget()
    did_work = False
    scenes: list[dict] = []
    for ep, path in enumerate(paths, 1):
        info = media.video_info(path)
        if did_work and budget.left() < 240:
            return _partial(pid, f"analyzed {ep - 1} of {len(paths)} episodes")
        log(f"episode {ep}/{len(paths)}: {path.name} ({fmt_time(info['duration'])})")
        cuts, cached = _detect_cuts(path, info, pdir / "work", ep)
        did_work = did_work or not cached
        spans = build_scenes(cuts, info["duration"])
        lines, source = _dialogue(path, info, pdir, ep, explicit[ep - 1] if ep - 1 < len(explicit) else None, whisper)
        first_id = len(scenes) + 1
        ep_scenes = [{
            "id": first_id + i, "episode": ep, "start": a, "end": b, "keyframe_time": round((a + b) / 2, 3),
            "dialogue": [], "description": "", "importance": None, "credits": False,
        } for i, (a, b) in enumerate(spans)]
        starts = [s["start"] for s in ep_scenes]
        for line in lines:
            index = max(0, bisect_right(starts, line["start"] + 0.05) - 1)
            ep_scenes[index]["dialogue"].append(line["text"])
        scenes.extend(ep_scenes)
        meta["videos"].append({
            "episode": ep, "path": str(path), "duration": info["duration"], "fps": info["fps"],
            "width": info["width"], "height": info["height"], "sar": info["sar"], "video_index": info["video_index"],
            "subtitle_source": source, "dialogue_lines": len(lines),
            "first_scene": first_id, "last_scene": len(scenes), "content_range": None,
        })
        log(f"episode {ep}: {len(ep_scenes)} scenes, {len(lines)} dialogue lines ({source})")
    write_json(pdir / "scenes.json", scenes)
    write_json(pdir / "project.json", meta)
    if not extract_keyframes(pdir, scenes, meta["videos"], budget):
        return _partial(pid, "scene detection done; keyframes not finished")
    meta["analyzed"] = True
    write_json(pdir / "project.json", meta)
    return project_summary(Project(pid))


def _partial(pid: str, progress: str) -> dict:
    return {"done": False, "project_id": pid, "progress": progress,
            "next": "Run the same analyze_video command again to continue (finished work is kept)."}


# ---------------------------------------------------------------- looking at scenes

def _ensure_keyframe(proj: Project, scene: dict) -> Path:
    path = proj.keyframe(scene)
    if not path.exists():
        video = proj.video(scene["episode"])
        _keyframe_job(Path(video["path"]), scene, path, video["sar"])
    return path


def get_contact_sheet(args: dict) -> dict:
    proj = Project.from_args(args)
    total = len(proj.scenes)
    first = int(args.get("first_scene", 1))
    count = max(1, min(int(args.get("count", 20)), 30))
    if not 1 <= first <= total:
        raise ToolError(f"first_scene must be between 1 and {total}.")
    chosen = proj.scenes[first - 1:first - 1 + count]
    last = chosen[-1]["id"]
    tiles = []
    for s in chosen:
        if s.get("credits"):
            bottom, bg = "CREDITS", (190, 30, 30)
        elif not proj.in_range(s):
            bottom, bg = "CUT", (120, 120, 120)
        elif s.get("importance"):
            bottom, bg = f"imp {s['importance']}", (20, 90, 160)
        else:
            bottom, bg = None, (0, 0, 0)
        tiles.append({"image": _ensure_keyframe(proj, s), "top": f"#{s['id']}  {proj.label_time(s)}",
                      "bottom": bottom, "bottom_bg": bg})
    cols = 5 if len(chosen) > 12 else min(4, len(chosen))
    sheet = grid_sheet(tiles, cols, 320, 180, f"{proj.id}  |  scenes {first}-{last} of {total}")
    out = proj.path("sheets", f"contact_{first:05d}_{last:05d}.jpg")
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, quality=88)
    rows = []
    for s in chosen:
        row = {"id": s["id"], "time": proj.label_time(s), "sec": round(s["end"] - s["start"], 1), "dialogue": s["dialogue"]}
        if s.get("description"):
            row.update(description=s["description"], importance=s.get("importance"))
        if s.get("credits"):
            row["credits"] = True
        rows.append(row)
    nxt = last + 1 if last < total else None
    return {
        "project_id": proj.id,
        "scenes_shown": f"{first}-{last}",
        "total_scenes": total,
        "scenes": rows,
        "next": (f"save_descriptions for scenes {first}-{last}, then get_contact_sheet with first_scene={nxt}"
                 if nxt else f"save_descriptions for scenes {first}-{last}. This was the last sheet."),
        "_images": [out],
    }


def get_scene_detail(args: dict) -> dict:
    proj = Project.from_args(args)
    scene = proj.scene(args.get("scene_id"))
    at = min(1.0, max(0.0, float(args.get("at", 0.5))))
    video = proj.video(scene["episode"])
    seconds = scene["start"] + (scene["end"] - scene["start"]) * at
    out = proj.path("frames", f"scene_{scene['id']:05d}_{int(at * 100):03d}.jpg")
    if not out.exists():
        media.extract_frame(Path(video["path"]), seconds, out, 1280, 720, video["sar"], quality=2)
    image = out
    if args.get("grid"):
        image = out.with_name(out.stem + "_grid.jpg")
        with Image.open(out) as im:
            draw_grid(im).save(image, quality=90)
    return {
        "scene": {
            "id": scene["id"], "episode": scene["episode"], "time": proj.label_time(scene),
            "start_sec": scene["start"], "end_sec": scene["end"], "frame_at_sec": round(seconds, 2),
            "dialogue": scene["dialogue"], "description": scene.get("description"),
            "importance": scene.get("importance"), "credits": scene.get("credits", False),
        },
        "_images": [image],
    }


def save_descriptions(args: dict) -> dict:
    proj = Project.from_args(args)
    items = args.get("descriptions") or args.get("scenes") or args.get("items")
    if not isinstance(items, list) or not items:
        raise ToolError("descriptions must be a non-empty list.",
                        hint='Example: {"project_id": "...", "descriptions": [{"id": 1, "description": "...", "importance": 3}]}')
    saved, warnings = [], []
    for item in items:
        if not isinstance(item, dict):
            warnings.append(f"skipped a non-object entry: {str(item)[:40]}")
            continue
        sid = item.get("id", item.get("scene_id"))
        scene = proj.get_scene(sid)
        if scene is None:
            warnings.append(f"scene {sid} does not exist")
            continue
        text = " ".join(str(item.get("description") or "").split())
        if text:
            scene["description"] = text
        if "credits" in item:
            scene["credits"] = bool(item["credits"])
        if item.get("importance") is not None:
            try:
                scene["importance"] = min(5, max(1, int(round(float(item["importance"])))))
            except (TypeError, ValueError):
                warnings.append(f"scene {sid}: importance must be 1-5")
        if not scene.get("description") and not scene.get("credits"):
            warnings.append(f"scene {sid}: no description")
        if not scene.get("importance") and not scene.get("credits"):
            warnings.append(f"scene {sid}: no importance (1-5)")
        saved.append(scene["id"])
    proj.save_scenes()
    pending = [s["id"] for s in proj.scenes if not (s.get("description") or s.get("credits"))]
    done_count = len(proj.scenes) - len(pending)
    return {
        "saved": len(saved),
        "described": done_count,
        "total": len(proj.scenes),
        "credits_scenes": sum(1 for s in proj.scenes if s.get("credits")),
        "warnings": warnings,
        "next": (f"get_contact_sheet with first_scene={pending[0]}" if pending
                 else "All scenes described. Next: save_notes, then write the script."),
    }


def set_content_range(args: dict) -> dict:
    proj = Project.from_args(args)
    episode = int(args.get("episode", 1))
    video = proj.video(episode)
    if args.get("clear"):
        video["content_range"] = None
    else:
        start = parse_time(args.get("start_sec", 0))
        end = min(parse_time(args.get("end_sec", video["duration"])), video["duration"])
        if end <= start:
            raise ToolError("end_sec must be after start_sec.")
        video["content_range"] = [round(start, 2), round(end, 2)]
    proj.save_meta()
    in_episode = [s for s in proj.scenes if s["episode"] == episode]
    inside = [s for s in in_episode if proj.in_range(s)]
    rng = video["content_range"]
    return {
        "episode": episode,
        "content_range": f"{fmt_time(rng[0])} - {fmt_time(rng[1])}" if rng else "whole episode",
        "scenes_in_range": f"{inside[0]['id']}-{inside[-1]['id']}" if inside else "none",
        "scenes_cut": len(in_episode) - len(inside),
        "usable_footage_minutes": round(usable_seconds(proj) / 60, 1),
    }


def save_notes(args: dict) -> dict:
    proj = Project.from_args(args)
    notes = str(args.get("notes") or "").strip()
    if not notes:
        raise ToolError("notes is required (plain text).")
    path = proj.path("notes.md")
    if args.get("append") and path.exists():
        notes = path.read_text(encoding="utf-8").rstrip() + "\n\n" + notes
    path.write_text(notes + "\n", encoding="utf-8")
    return {"saved_chars": len(notes), "path": str(path)}


def project_status(proj: Project) -> dict:
    status: dict = {"analyzed": bool(proj.meta.get("analyzed"))}
    if not status["analyzed"]:
        status["next_step"] = "Run analyze_video again to finish the analysis."
        return status
    pending = [s["id"] for s in proj.scenes if not (s.get("description") or s.get("credits"))]
    script = proj.load_script()
    beats = script.get("beats", [])
    voice = proj.load_voice()
    status.update({
        "scenes": len(proj.scenes),
        "described": len(proj.scenes) - len(pending),
        "next_undescribed_scene": pending[0] if pending else None,
        "target_minutes": target_minutes(proj, script),
        "script_beats": len(beats),
        "script_words": sum(word_count(b["text"]) for b in beats),
        "script_finished": bool(beats) and beats[-1]["text"].endswith(CLOSING_LINE),
        "voice_beats": len(voice.get("beats", [])),
        "video": str(proj.path("output", "recap.mp4")) if proj.path("output", "recap.mp4").exists() else None,
        "thumbnail": str(proj.path("output", "thumbnail.jpg")) if proj.path("output", "thumbnail.jpg").exists() else None,
        "publish_info": str(proj.path("output", "publish.txt")) if proj.path("output", "publish.txt").exists() else None,
    })
    if pending:
        status["next_step"] = f"get_contact_sheet with first_scene={pending[0]}"
    elif not status["script_finished"]:
        status["next_step"] = "write the script (save_script with append=true)"
    elif not status["video"]:
        status["next_step"] = "generate_voice, then render_video"
    elif not status["thumbnail"]:
        status["next_step"] = "get_preview_sheet checks, then the thumbnail"
    elif not status["publish_info"]:
        status["next_step"] = "save_publish_info"
    else:
        status["next_step"] = "done: report the output files to the user"
    return status


def get_notes(args: dict) -> dict:
    proj = Project.from_args(args)
    path = proj.path("notes.md")
    return {"notes": path.read_text(encoding="utf-8") if path.exists() else "",
            "status": project_status(proj)}


def get_scenes_text(args: dict) -> dict:
    proj = Project.from_args(args)
    total = len(proj.scenes)
    first = int(args.get("first_scene", 1))
    count = max(1, min(int(args.get("count", 40)), 150))
    if not 1 <= first <= total:
        raise ToolError(f"first_scene must be between 1 and {total}.")
    lines = []
    for s in proj.scenes[first - 1:first - 1 + count]:
        flag = " [CREDITS]" if s.get("credits") else ("" if proj.in_range(s) else " [CUT]")
        imp = f" imp {s['importance']}" if s.get("importance") else ""
        line = f"#{s['id']} {proj.label_time(s)} ({s['end'] - s['start']:.1f}s){imp}{flag}: {s.get('description') or '(not described)'}"
        if s["dialogue"]:
            line += "  | dialogue: " + " / ".join(s["dialogue"])
        lines.append(line)
    last = first + len(lines) - 1
    return {"scenes": f"{first}-{last} of {total}", "text": "\n".join(lines),
            "next_first_scene": last + 1 if last < total else None}


def list_projects(args: dict) -> dict:
    found, seen = [], set()
    for base in candidate_homes():
        if not base.is_dir():
            continue
        for folder in sorted(base.iterdir()):
            meta = read_json(folder / "project.json", None)
            if not meta or meta.get("project_id") in seen:
                continue
            seen.add(meta["project_id"])
            entry = {"project_id": meta["project_id"], "dir": str(folder), "created": meta.get("created"),
                     "videos": [Path(v["path"]).name for v in meta.get("videos", [])]}
            try:
                entry["status"] = project_status(Project(meta["project_id"]))
            except ToolError as exc:
                entry["status"] = {"error": str(exc)}
            found.append(entry)
    return {"projects": found}
