"""Rendering: match clips to beats, cut and concatenate them under the narration; plus the preview sheet."""
from __future__ import annotations

import json
import math
import random
import wave
from concurrent.futures import ThreadPoolExecutor, as_completed
from fractions import Fraction
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from .common import (Budget, Project, ToolError, compact_ids, fmt_time, log, read_json, segment_durations,
                     short_hash, write_json)
from .config import find_logo, find_music, load_settings
from .media import fit_filter, probe, run, run_ffmpeg
from .script import beat_rows, name_card_plan, name_pattern, script_key
from .sheets import draw_tag, font_path, get_font, load_tile, wrap
from .voice import SAMPLE_RATE, voice_durations, voice_wavs

OUT_W, OUT_H = 1920, 1080
MAX_CLIP_SEC = 5.0      # longest cut from one scene (relaxed only when footage runs out)
MIN_CLIP_SEC = 1.5      # fewer, longer clips when a beat lists more scenes than it can show
MIN_SPEED = 0.85        # a beat a little short of footage plays slightly slower instead of freezing
EDGE_SEC = 0.02         # a hair inside each shot so rounding never flashes the neighbouring shot
X264 = ["-c:v", "libx264", "-preset", "veryfast", "-tune", "animation", "-crf", "20", "-pix_fmt", "yuv420p"]
CORNER = 0.03           # gap between the logo / name cards and the frame edge, as a fraction of the height
CROSSFADE_SEC = 2.0     # between background music tracks
RENDER_OPTIONS = ("logo", "logo_size", "logo_opacity", "music", "music_volume", "music_shuffle",
                  "character_names", "name_seconds")


def output_fps(proj: Project) -> Fraction:
    fps = proj.video(1)["fps"]
    for rate in (Fraction(24000, 1001), Fraction(24), Fraction(25), Fraction(30000, 1001), Fraction(30)):
        if abs(fps - float(rate)) < 0.05:
            return rate
    return Fraction(30)


def water_fill(caps: list[float], total: float) -> list[float]:
    """Share `total` seconds as evenly as possible without giving any clip more than its cap."""
    alloc = [0.0] * len(caps)
    open_idx = list(range(len(caps)))
    remaining = total
    while open_idx and remaining > 1e-6:
        share = remaining / len(open_idx)
        capped = [k for k in open_idx if caps[k] - alloc[k] <= share]
        if not capped:
            for k in open_idx:
                alloc[k] += share
            break
        for k in capped:
            remaining -= caps[k] - alloc[k]
            alloc[k] = caps[k]
        open_idx = [k for k in open_idx if k not in capped]
    return alloc


def _clip_cap(scene: dict, longest: float) -> float:
    return max(0.0, min(scene["end"] - scene["start"] - 2 * EDGE_SEC, longest))


def _fillers(proj: Project, chosen: list[int], last_shown: int, is_free) -> list[int]:
    """Unlisted scenes inside the beat's span, then the scenes right after it, then right before it."""
    lo, hi = (min(chosen), max(chosen)) if chosen else (last_shown, last_shown)
    anchor = proj.get_scene(hi)
    episode = anchor["episode"] if anchor else None
    out = [sid for sid in range(lo + 1, hi) if is_free(sid)]
    for step in (1, -1):
        sid = hi + 1 if step == 1 else lo - 1
        for _ in range(40):
            scene = proj.get_scene(sid)
            if scene is None or (episode and scene["episode"] != episode):
                break
            if is_free(sid):
                out.append(sid)
            elif proj.usable(scene):
                break  # this footage belongs to another beat
            sid += step
    return out


def _ordered(listed: list[int], fillers: list[int]) -> list[int]:
    """Keep the script's order; put each filler after the last listed scene that comes before it."""
    result = list(listed)
    for f in sorted(fillers):
        pos = 0
        for k, sid in enumerate(result):
            if sid < f:
                pos = k + 1
        result.insert(pos, f)
    return result


def plan_beats(proj: Project, beats: list[dict], durations: list[float]) -> list[dict]:
    """Choose each beat's clips: its scenes in order, max ~5 s each, never reusing a scene.

    A short beat first takes free nearby scenes, then the boundary scene of a neighbouring beat
    that has footage to spare, then longer clips, then plays slightly slower (down to MIN_SPEED);
    only then is its last frame held.
    """
    count = len(beats)
    owner: dict[int, int] = {}          # each usable scene belongs to the first beat that lists it
    listed: list[list[int]] = []
    for i, beat in enumerate(beats):
        ids = []
        for sid in beat["scene_ids"]:
            scene = proj.get_scene(sid)
            if scene is not None and proj.usable(scene) and sid not in owner:
                owner[sid] = i
                ids.append(sid)
        listed.append(ids)
    notes: list[list[str]] = [[] for _ in beats]
    for i, ids in enumerate(listed):
        limit = max(1, int(durations[i] / MIN_CLIP_SEC))
        if len(ids) > limit:  # too many scenes for the narration: keep the most important
            ranked = sorted(ids, key=lambda s: (-(proj.scene(s).get("importance") or 3), ids.index(s)))
            keep = set(ranked[:limit])
            for sid in ids:
                if sid not in keep:
                    owner.pop(sid)
            notes[i].append(f"{len(ids) - limit} listed scenes left out (too many for {durations[i]:.0f}s)")
            listed[i] = [s for s in ids if s in keep]
    members = [list(ids) for ids in listed]

    def cap(sid: int, longest: float) -> float:
        return _clip_cap(proj.scene(sid), longest)

    def capacity(i: int, longest: float) -> float:
        return sum(cap(s, longest) for s in members[i])

    def short(i: int, longest: float) -> bool:
        return capacity(i, longest) < durations[i] - 0.01

    taken = set(owner)

    def is_free(sid: int) -> bool:
        scene = proj.get_scene(sid)
        return scene is not None and proj.usable(scene) and sid not in taken

    last_shown = 0
    for i in range(count):
        if short(i, MAX_CLIP_SEC):
            for sid in _fillers(proj, members[i], last_shown, is_free):
                members[i].append(sid)
                taken.add(sid)
                if not short(i, MAX_CLIP_SEC):
                    break
        if members[i]:
            last_shown = max(last_shown, max(members[i]))

    for longest in (MAX_CLIP_SEC, math.inf):
        for _ in range(3):
            moved = False
            for i in range(count - 1):
                a, b = members[i], members[i + 1]
                while short(i, longest) and len(b) > 1:
                    sid = min(b)
                    if (a and sid < max(a)) or capacity(i + 1, longest) - cap(sid, longest) < durations[i + 1]:
                        break
                    b.remove(sid)
                    a.append(sid)
                    moved = True
                    notes[i].append(f"borrowed scene {sid} from beat {i + 2}")
                while short(i + 1, longest) and len(a) > 1:
                    sid = max(a)
                    if (b and sid > min(b)) or capacity(i, longest) - cap(sid, longest) < durations[i]:
                        break
                    a.remove(sid)
                    b.append(sid)
                    moved = True
                    notes[i + 1].append(f"borrowed scene {sid} from beat {i + 1}")
            if not moved:
                break

    plans = []
    for i in range(count):
        target = durations[i]
        longest = MAX_CLIP_SEC if not short(i, MAX_CLIP_SEC) else math.inf
        if longest == math.inf and members[i]:
            notes[i].append("some clips are longer than 5 s")
        mine = set(listed[i])
        order = _ordered([s for s in listed[i] if s in members[i]], [s for s in members[i] if s not in mine])
        have = capacity(i, longest)
        speed = max(MIN_SPEED, have / target) if 0 < have < target else 1.0
        if speed < 1.0:
            notes[i].append(f"footage slowed to {speed:.0%} to fill the narration")
        alloc = water_fill([cap(s, longest) for s in order], min(target * speed, have))
        clips, at = [], 0.0
        for sid, seconds in zip(order, alloc):
            if seconds < 0.05:
                continue
            scene = proj.scene(sid)
            start = scene["start"] + (scene["end"] - scene["start"] - seconds) / 2   # centred in the shot
            clips.append({"scene": sid, "episode": scene["episode"], "src_in": round(start, 3),
                          "src_dur": round(seconds, 3), "speed": round(speed, 4),
                          "duration": round(seconds / speed, 3), "at": round(at, 3)})
            at += seconds / speed
        freeze = max(0.0, target - at)
        warnings = []
        if freeze > 0.5:
            warnings.append(f"only {at:.1f}s of footage for {target:.1f}s, so the last frame is held {freeze:.1f}s; "
                            "add scene_ids or shorten the text")
        plans.append({"beat": i + 1, "duration": round(target, 3), "clips": clips, "freeze": round(freeze, 2),
                      "notes": notes[i], "warnings": warnings})
    return plans


# ---------------------------------------------------------------- rendering

# ---------------------------------------------------------------- overlays: channel logo and name cards

def _prepare_logo(proj: Project, source: Path, settings: dict) -> dict:
    """Scale the channel logo for the bottom-right corner and bake in its opacity."""
    stat = source.stat()
    key = short_hash(source, stat.st_size, int(stat.st_mtime), settings["logo_size"], settings["logo_opacity"])
    out = proj.path("render", f"logo_{key}.png")
    with Image.open(source) as im:
        logo = im.convert("RGBA")
    width = round(OUT_W * settings["logo_size"])
    height = round(logo.height * width / logo.width)
    if height > OUT_H * 0.25:
        height = round(OUT_H * 0.25)
        width = round(logo.width * height / logo.height)
    if not out.exists():
        logo = logo.resize((width, height), Image.LANCZOS)
        if settings["logo_opacity"] < 1:
            logo.putalpha(logo.getchannel("A").point(lambda a: int(a * settings["logo_opacity"])))
        out.parent.mkdir(parents=True, exist_ok=True)
        logo.save(out)
    margin = round(OUT_H * CORNER)
    return {"path": str(out), "w": width, "h": height, "x": OUT_W - width - margin, "y": OUT_H - height - margin}


def _name_card(proj: Project, name: str) -> dict:
    """The character's name in bold white letters with a black outline and soft shadow (transparent PNG)."""
    size = round(OUT_H * 0.065)
    stroke = max(3, size // 11)
    pad = 12
    font = get_font(size, heavy=True)
    left, top, right, bottom = ImageDraw.Draw(Image.new("RGBA", (1, 1))).textbbox((0, 0), name, font=font, stroke_width=stroke)
    w, h = math.ceil(right - left) + 2 * pad, math.ceil(bottom - top) + 2 * pad
    out = proj.path("render", f"name_{short_hash(name, size, font_path(True))}.png")
    if not out.exists():
        origin = (pad - left, pad - top)
        shadow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ImageDraw.Draw(shadow).text((origin[0] + 3, origin[1] + 4), name, font=font, fill=(0, 0, 0, 180),
                                    stroke_width=stroke, stroke_fill=(0, 0, 0, 180))
        card = shadow.filter(ImageFilter.GaussianBlur(3))
        ImageDraw.Draw(card).text(origin, name, font=font, fill=(255, 255, 255, 255),
                                  stroke_width=stroke, stroke_fill=(0, 0, 0, 255))
        out.parent.mkdir(parents=True, exist_ok=True)
        card.save(out)
    return {"path": str(out), "w": w, "h": h}


def _card_window(text: str, item: dict, plan: dict, voice_sec: float, seg_sec: float, show: float) -> tuple[float, float]:
    """When a name card shows inside its beat: on the given scene's clip, else when the narration says the name."""
    start = None
    if item.get("scene") is not None:
        clip = next((c for c in plan["clips"] if c["scene"] == item["scene"]), None)
        if clip:
            start = clip["at"] + 0.3
    if start is None:
        match = name_pattern(item["name"]).search(text)
        start = voice_sec * len(text[:match.start()].split()) / max(1, len(text.split())) - 0.15 if match else 0.5
    show = min(show, max(0.6, seg_sec - 0.2))
    start = max(0.0, min(start, seg_sec - 0.1 - show))
    return round(start, 3), round(start + show, 3)


def _place_cards(proj: Project, beats: list[dict], plans: list[dict], spoken: list[float], seg_secs: list[float],
                 settings: dict, logo: dict | None) -> list[list[dict]]:
    """Name cards per beat, right-aligned at the bottom right (above the logo); overlapping cards stack upward."""
    out: list[list[dict]] = [[] for _ in beats]
    if not settings["character_names"]:
        return out
    margin, gap = round(OUT_H * CORNER), round(OUT_H * 0.012)
    bottom = logo["y"] - gap if logo else OUT_H - margin
    for i, items in name_card_plan(proj, beats).items():
        for item in items:
            card = _name_card(proj, item["name"])
            start, end = _card_window(beats[i]["text"], item, plans[i], spoken[i], seg_secs[i], settings["name_seconds"])
            slot = sum(1 for c in out[i] if c["start"] < end and start < c["end"])
            out[i].append({**card, "name": item["name"], "start": start, "end": end,
                           "x": OUT_W - margin - card["w"], "y": bottom - card["h"] - slot * (card["h"] + gap)})
    return out


# ---------------------------------------------------------------- background music

def _music_inputs(proj: Project, tracks: list[Path], total: float, shuffle: bool) -> tuple[list[str], int]:
    """ffmpeg inputs that cover `total` seconds: one track loops; several play in turn with crossfades."""
    usable = []
    for track in tracks:
        try:
            seconds = float((probe(track).get("format") or {}).get("duration") or 0)
        except ToolError:
            seconds = 0.0
        if seconds > 2 * CROSSFADE_SEC + 1:
            usable.append((track, seconds))
    if shuffle:
        random.Random(proj.id).shuffle(usable)
    if not usable:
        return [], 0
    if len(usable) == 1:
        return ["-stream_loop", "-1", "-i", str(usable[0][0])], 1
    args: list[str] = []
    count, covered = 0, 0.0
    while covered < total + 1 and count < 200:
        for track, seconds in usable:
            args += ["-i", str(track)]
            covered += seconds - (CROSSFADE_SEC if count else 0)
            count += 1
            if covered >= total + 1 or count >= 200:
                break
    return args, count


def _audio_graph(music_count: int, total: float, volume: float) -> str:
    """Narration (input 1) in stereo at -14 LUFS, with the music (inputs 2...) normalised and mixed under it."""
    voice = "[1:a]pan=stereo|c0=c0|c1=c0,loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000"
    if not music_count or volume <= 0:
        return voice + "[a]"
    fmt = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
    parts = [f"{voice},{fmt}[voice]"] + [f"[{2 + k}:a]{fmt}[m{k}]" for k in range(music_count)]
    current = "[m0]"
    for k in range(1, music_count):
        parts.append(f"{current}[m{k}]acrossfade=d={CROSSFADE_SEC}:c1=tri:c2=tri[x{k}]")
        current = f"[x{k}]"
    parts.append(f"{current}atrim=0:{total:.3f},asetpts=PTS-STARTPTS,loudnorm=I=-14:TP=-2:LRA=11,{fmt},"
                 f"volume={volume:.3f},afade=t=in:st=0:d=2,afade=t=out:st={max(0.0, total - 3):.3f}:d=3[music]")
    parts.append("[voice][music]amerge=inputs=2,pan=stereo|c0=c0+c2|c1=c1+c3,alimiter=limit=0.95[a]")
    return ";".join(parts)


# ---------------------------------------------------------------- rendering

def _render_segment(proj: Project, plan: dict, frames: int, fps: Fraction, out: Path,
                    logo: dict | None = None, cards: list[dict] = ()) -> None:
    inputs, chains, labels = [], [], []
    for clip in plan["clips"]:
        first = round(clip["at"] * fps)
        count = round((clip["at"] + clip["duration"]) * fps) - first
        if count <= 0:
            continue
        video = proj.video(clip["episode"])
        k = len(labels)
        speed = clip.get("speed", 1.0)
        slow = f"setpts=(PTS-STARTPTS)/{speed:.4f}," if speed < 0.9999 else ""
        inputs += ["-ss", f"{clip['src_in']:.3f}", "-t", f"{clip.get('src_dur', clip['duration']) + 0.5:.3f}",
                   "-i", video["path"]]
        chains.append(f"[{k}:{video['video_index']}]{fit_filter(OUT_W, OUT_H, video['sar'])},{slow}fps={fps},"
                      f"trim=end_frame={count},setpts=PTS-STARTPTS[c{k}]")
        labels.append(f"[c{k}]")
    seconds = float(frames / fps)
    if labels:
        # tpad holds the last frame if the clips come up short; -frames:v cuts to the exact length.
        chains.append(f"{''.join(labels)}concat=n={len(labels)}:v=1:a=0,format=yuv420p,"
                      f"tpad=stop_mode=clone:stop_duration={seconds + 1:.3f}[base]")
    else:
        inputs = ["-f", "lavfi", "-i", f"color=c=black:s={OUT_W}x{OUT_H}:r={fps}"]
        chains = ["[0:v]format=yuv420p[base]"]
    current, index = "[base]", max(1, len(labels))
    if logo:  # a still image: overlay repeats its only frame for the whole segment
        inputs += ["-i", logo["path"]]
        chains.append(f"{current}[{index}:v]overlay=x={logo['x']}:y={logo['y']}[lg]")
        current, index = "[lg]", index + 1
    for j, card in enumerate(cards):
        inputs += ["-loop", "1", "-framerate", str(fps), "-t", f"{seconds:.3f}", "-i", card["path"]]
        fade_out = max(card["start"] + 0.25, card["end"] - 0.35)
        chains.append(f"[{index}:v]format=rgba,fade=t=in:st={card['start']:.3f}:d=0.25:alpha=1,"
                      f"fade=t=out:st={fade_out:.3f}:d=0.35:alpha=1[nc{j}]")
        chains.append(f"{current}[nc{j}]overlay=x={card['x']}:y={card['y']}[ov{j}]")
        current, index = f"[ov{j}]", index + 1
    chains.append(f"{current}format=yuv420p[v]")
    tmp = out.with_name(out.stem + ".part.mp4")
    run(["ffmpeg", "-nostdin", "-v", "error", "-y", *inputs, "-filter_complex", ";".join(chains), "-map", "[v]",
         "-frames:v", str(frames), "-an", "-sn", "-dn", *X264, "-r", str(fps), tmp])
    tmp.replace(out)


def _build_narration(proj: Project, beats: list[dict], bounds: list[int], fps: Fraction, out: Path) -> None:
    """One WAV with each beat's voice placed exactly where its video segment starts."""
    wavs = voice_wavs(proj)
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        pos = 0
        for n in range(1, len(beats) + 1):
            start = round(bounds[n - 1] / fps * SAMPLE_RATE)
            if start > pos:
                w.writeframes(b"\x00\x00" * (start - pos))
                pos = start
            with wave.open(str(wavs[n]), "rb") as r:
                data = r.readframes(r.getnframes())
            w.writeframes(data)
            pos += len(data) // 2
        end = round(bounds[-1] / fps * SAMPLE_RATE)
        if end > pos:
            w.writeframes(b"\x00\x00" * (end - pos))


def _concat_line(path: Path) -> str:
    return "file '" + path.as_posix().replace("'", "'\\''") + "'\n"


def render_video(args: dict) -> dict:
    proj = Project.from_args(args)
    beats = proj.load_script()["beats"]
    if not beats:
        raise ToolError("There is no script yet.", hint="Write it with save_script first.")
    spoken = voice_durations(proj, beats)
    missing = [n for n, d in enumerate(spoken, 1) if d is None]
    if missing:
        raise ToolError(f"Narration is missing or out of date for beats {compact_ids(missing)}.",
                        hint="Run generate_voice first (only changed beats are regenerated).")
    fps = output_fps(proj)
    bounds, t = [0], 0.0
    for seconds in segment_durations(spoken):
        t += seconds
        bounds.append(round(t * fps))
    frames = [b - a for a, b in zip(bounds, bounds[1:])]
    seg_secs = [float(f / fps) for f in frames]
    plans = plan_beats(proj, beats, seg_secs)
    settings = load_settings({k: args[k] for k in RENDER_OPTIONS if k in args})
    notes: list[str] = []
    logo_source, logo_note = find_logo(settings)
    logo = _prepare_logo(proj, logo_source, settings) if logo_source else None
    if logo_note.startswith("missing"):
        notes.append(f"Channel logo {logo_note}; rendering without it.")
    tracks, music_note = find_music(settings)
    if music_note.startswith("missing"):
        notes.append(f"Background music {music_note}; rendering without it.")
    cards = _place_cards(proj, beats, plans, spoken, seg_secs, settings, logo)

    render_dir = proj.path("render")
    render_dir.mkdir(parents=True, exist_ok=True)
    segments, jobs = [], []
    for plan, count, beat_cards in zip(plans, frames, cards):
        key = short_hash(json.dumps(plan["clips"]), count, fps, X264, OUT_W, OUT_H,
                         json.dumps(logo), json.dumps(beat_cards))
        path = render_dir / f"seg_{plan['beat']:03d}_{key}.mp4"
        segments.append(path)
        if not path.exists():
            jobs.append((plan, count, path, beat_cards))
    log(f"render: {len(jobs)} of {len(segments)} beat segments to encode")
    budget = Budget()
    finished = True
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_render_segment, proj, plan, count, fps, path, logo, beat_cards)
                   for plan, count, path, beat_cards in jobs]
        for done, future in enumerate(as_completed(futures), 1):
            future.result()
            if done % 5 == 0 or done == len(futures):
                log(f"render: segment {done}/{len(futures)}")
            if budget.expired(reserve=60):
                pool.shutdown(wait=True, cancel_futures=True)
                finished = False
                break
    if not finished:
        ready = sum(1 for p in segments if p.exists())
        return {"done": False, "progress": f"{ready}/{len(segments)} beat segments encoded",
                "next": "Run render_video again to continue (finished segments are kept)."}
    keep = {p.name for p in segments}
    for old in render_dir.glob("seg_*.mp4"):
        if old.name not in keep:
            old.unlink()

    total = float(bounds[-1] / fps)
    narration = render_dir / "narration.wav"
    _build_narration(proj, beats, bounds, fps, narration)
    listing = render_dir / "segments.txt"
    listing.write_text("".join(_concat_line(p) for p in segments), encoding="utf-8")
    final = proj.path("output", "recap.mp4")
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = final.with_name("recap.part.mp4")
    music_args, music_count = _music_inputs(proj, tracks, total, settings["music_shuffle"]) if tracks else ([], 0)
    if tracks and not music_count:
        notes.append("Background music files are unreadable or under 5 s; rendering without music.")
    # No subtitle or data streams: the recap never carries subtitles or captions.
    run_ffmpeg(["-f", "concat", "-safe", "0", "-i", listing, "-i", narration, *music_args,
                "-filter_complex", _audio_graph(music_count, total, settings["music_volume"]),
                "-map", "0:v:0", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
                "-sn", "-dn", "-map_chapters", "-1", "-movflags", "+faststart", tmp],
               duration=total, label="final mux")
    tmp.replace(final)
    used_music = [Path(arg).name for flag, arg in zip(music_args, music_args[1:]) if flag == "-i"]

    timeline_beats = []
    for n, (beat, plan) in enumerate(zip(beats, plans), 1):
        start, end = float(bounds[n - 1] / fps), float(bounds[n] / fps)
        timeline_beats.append({
            "beat": n, "start": round(start, 3), "end": round(end, 3), "start_tc": fmt_time(start),
            "voice_sec": spoken[n - 1], "text": beat["text"], "scene_ids": beat["scene_ids"],
            "clips": [{**c, "video_at": round(start + c["at"], 3)} for c in plan["clips"]],
            "names": [{"name": c["name"], "start": round(start + c["start"], 3), "end": round(start + c["end"], 3)}
                      for c in cards[n - 1]],
            "freeze_sec": plan["freeze"], "notes": plan["notes"] + plan["warnings"],
        })
    timeline_path = proj.path("output", "timeline.json")
    write_json(timeline_path, {"project_id": proj.id, "video": str(final), "duration": round(total, 3),
                               "fps": str(fps), "script_key": script_key(beats), "logo": logo_note if logo else None,
                               "music": list(dict.fromkeys(used_music)), "music_volume": settings["music_volume"] if used_music else None,
                               "subtitles": "none", "beats": timeline_beats})
    actual = float((probe(final).get("format") or {}).get("duration") or 0)
    warnings = notes + [f"Beat {p['beat']}: {w}" for p in plans for w in p["warnings"]]
    if abs(actual - total) > 0.5:
        warnings.append(f"Video is {actual:.1f}s but the timeline expects {total:.1f}s.")
    return {
        "video": str(final),
        "duration": fmt_time(actual or total),
        "duration_sec": round(actual or total, 2),
        "resolution": f"{OUT_W}x{OUT_H}",
        "fps": str(fps),
        "beats": len(beats),
        "clips": sum(len(p["clips"]) for p in plans),
        "logo": logo_note if logo else f"none ({logo_note})",
        "music": (f"{len(set(used_music))} track(s) at volume {settings['music_volume']:g}" if used_music
                  else f"none ({music_note})"),
        "name_cards": [f"{c['name']} at {fmt_time(float(bounds[i] / fps) + c['start'])}"
                       for i, beat_cards in enumerate(cards) for c in beat_cards],
        "subtitles": "none",
        "timeline": str(timeline_path),
        "warnings": warnings,
        "next": "get_preview_sheet (first_beat=1, 9, 17, ...) and fix wrong clips with update_beats, then render again.",
    }


# ---------------------------------------------------------------- preview sheet

THUMB_W, THUMB_H = 176, 99
PANEL_W = 440
MAX_THUMBS = 8


def get_preview_sheet(args: dict) -> dict:
    proj = Project.from_args(args)
    beats = proj.load_script()["beats"]
    if not beats:
        raise ToolError("There is no script yet.", hint="Write it with save_script first.")
    first = int(args.get("first_beat", 1))
    count = max(1, min(int(args.get("count", 8)), 12))
    if not 1 <= first <= len(beats):
        raise ToolError(f"first_beat must be between 1 and {len(beats)}.")
    timeline = read_json(proj.path("output", "timeline.json"), None)
    if timeline and timeline.get("script_key") == script_key(beats):
        rows = [{"beat": b["beat"], "start": b["start"], "clips": b["clips"], "freeze": b["freeze_sec"],
                 "notes": b["notes"], "names": [c["name"] for c in b.get("names", [])]} for b in timeline["beats"]]
        source = "rendered video"
    else:
        timing, how = beat_rows(proj, beats)
        plans = plan_beats(proj, beats, [r["end"] - r["start"] for r in timing])
        names = name_card_plan(proj, beats) if load_settings()["character_names"] else {}
        rows = [{"beat": p["beat"], "start": r["start"], "clips": p["clips"], "freeze": p["freeze"],
                 "notes": p["notes"] + p["warnings"], "names": [c["name"] for c in names.get(p["beat"] - 1, [])]}
                for p, r in zip(plans, timing)]
        source = f"plan before rendering ({how})"
    rows = rows[first - 1:first - 1 + count]

    row_h = THUMB_H + 16
    width = PANEL_W + MAX_THUMBS * (THUMB_W + 6) + 10
    sheet = Image.new("RGB", (width, 40 + len(rows) * (row_h + 8)), (24, 24, 24))
    draw = ImageDraw.Draw(sheet)
    draw.text((10, 9), f"{proj.id}  |  beats {rows[0]['beat']}-{rows[-1]['beat']} of {len(beats)}  |  {source}",
              font=get_font(20), fill=(235, 235, 235))
    head_font, text_font, tag_font = get_font(19), get_font(15), get_font(14)
    out_rows = []
    for k, row in enumerate(rows):
        y = 40 + k * (row_h + 8)
        beat = beats[row["beat"] - 1]
        draw.rectangle([4, y, width - 4, y + row_h], fill=(36, 36, 36))
        draw.text((12, y + 4), f"Beat {row['beat']}  {fmt_time(row['start'])}", font=head_font, fill=(255, 235, 59))
        if row["names"]:
            draw_tag(draw, (190, y + 4), "NAME: " + ", ".join(row["names"]), tag_font, fill=(255, 255, 255), bg=(20, 90, 160))
        for j, line in enumerate(wrap(draw, beat["text"], text_font, PANEL_W - 24, max_lines=4)):
            draw.text((12, y + 28 + j * 19), line, font=text_font, fill=(225, 225, 225))
        clips = row["clips"]
        shown = clips[:MAX_THUMBS]
        for j, clip in enumerate(shown):
            x = PANEL_W + j * (THUMB_W + 6)
            scene = proj.scene(clip["scene"])
            sheet.paste(load_tile(proj.keyframe(scene), (THUMB_W, THUMB_H)), (x, y + 8))
            draw_tag(draw, (x, y + 8), f"#{clip['scene']} {clip['duration']:.1f}s", tag_font, fill=(255, 235, 59))
        if len(clips) > MAX_THUMBS:
            x = PANEL_W + (MAX_THUMBS - 1) * (THUMB_W + 6)
            draw_tag(draw, (x, y + 8 + THUMB_H), f"+{len(clips) - MAX_THUMBS} more", tag_font, anchor_bottom=True)
        if row["freeze"] > 0.1:
            x = PANEL_W + max(0, len(shown) - 1) * (THUMB_W + 6)
            draw_tag(draw, (x + THUMB_W - 90, y + 8 + THUMB_H), f"HOLD {row['freeze']:.1f}s", tag_font,
                     bg=(190, 30, 30), anchor_bottom=True)
        out_rows.append({
            "beat": row["beat"], "time": fmt_time(row["start"]), "text": beat["text"],
            "shows": [{"scene": c["scene"], "sec": c["duration"],
                       "description": proj.scene(c["scene"]).get("description") or ""} for c in clips],
            "names": row["names"],
            "notes": row["notes"],
        })
    out = proj.path("sheets", f"preview_{rows[0]['beat']:03d}_{rows[-1]['beat']:03d}.jpg")
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, quality=88)
    last = rows[-1]["beat"]
    return {
        "source": source,
        "beats": out_rows,
        "next": (f"get_preview_sheet with first_beat={last + 1}" if last < len(beats)
                 else "Last page. Fix wrong clips with update_beats, then render_video again."),
        "_images": [out],
    }
