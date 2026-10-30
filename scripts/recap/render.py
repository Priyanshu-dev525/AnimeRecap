"""Rendering: match clips to beats, cut and concatenate them under the narration; plus the preview sheet."""
from __future__ import annotations

import json
import math
import wave
from concurrent.futures import ThreadPoolExecutor, as_completed
from fractions import Fraction
from pathlib import Path

from PIL import Image, ImageDraw

from .common import (Budget, Project, ToolError, compact_ids, fmt_time, log, read_json, segment_durations,
                     short_hash, write_json)
from .media import fit_filter, probe, run, run_ffmpeg
from .script import beat_rows, script_key
from .sheets import draw_tag, get_font, load_tile, wrap
from .voice import SAMPLE_RATE, voice_durations, voice_wavs

OUT_W, OUT_H = 1920, 1080
MAX_CLIP_SEC = 5.0      # longest cut from one scene (relaxed only when footage runs out)
MIN_CLIP_SEC = 1.5      # fewer, longer clips when a beat lists more scenes than it can show
MIN_SPEED = 0.85        # a beat a little short of footage plays slightly slower instead of freezing
EDGE_SEC = 0.02         # a hair inside each shot so rounding never flashes the neighbouring shot
X264 = ["-c:v", "libx264", "-preset", "veryfast", "-tune", "animation", "-crf", "20", "-pix_fmt", "yuv420p"]


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

def _render_segment(proj: Project, plan: dict, frames: int, fps: Fraction, out: Path) -> None:
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
    if labels:
        # tpad holds the last frame if the clips come up short; -frames:v cuts to the exact length.
        graph = ";".join(chains + [f"{''.join(labels)}concat=n={len(labels)}:v=1:a=0,format=yuv420p,"
                                   f"tpad=stop_mode=clone:stop_duration={float(frames / fps) + 1:.3f}[v]"])
    else:
        inputs = ["-f", "lavfi", "-i", f"color=c=black:s={OUT_W}x{OUT_H}:r={fps}"]
        graph = "[0:v]format=yuv420p[v]"
    tmp = out.with_name(out.stem + ".part.mp4")
    run(["ffmpeg", "-nostdin", "-v", "error", "-y", *inputs, "-filter_complex", graph, "-map", "[v]",
         "-frames:v", str(frames), "-an", *X264, "-r", str(fps), tmp])
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
    plans = plan_beats(proj, beats, [float(f / fps) for f in frames])

    render_dir = proj.path("render")
    render_dir.mkdir(parents=True, exist_ok=True)
    segments, jobs = [], []
    for plan, count in zip(plans, frames):
        key = short_hash(json.dumps(plan["clips"]), count, fps, X264, OUT_W, OUT_H)
        path = render_dir / f"seg_{plan['beat']:03d}_{key}.mp4"
        segments.append(path)
        if not path.exists():
            jobs.append((plan, count, path))
    log(f"render: {len(jobs)} of {len(segments)} beat segments to encode")
    budget = Budget()
    finished = True
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_render_segment, proj, plan, count, fps, path) for plan, count, path in jobs]
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
    run_ffmpeg(["-f", "concat", "-safe", "0", "-i", listing, "-i", narration, "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "copy", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11", "-ar", "48000", "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart", tmp], duration=float(total), label="final mux")
    tmp.replace(final)

    timeline_beats = []
    for n, (beat, plan) in enumerate(zip(beats, plans), 1):
        start, end = float(bounds[n - 1] / fps), float(bounds[n] / fps)
        timeline_beats.append({
            "beat": n, "start": round(start, 3), "end": round(end, 3), "start_tc": fmt_time(start),
            "voice_sec": spoken[n - 1], "text": beat["text"], "scene_ids": beat["scene_ids"],
            "clips": [{**c, "video_at": round(start + c["at"], 3)} for c in plan["clips"]],
            "freeze_sec": plan["freeze"], "notes": plan["notes"] + plan["warnings"],
        })
    timeline_path = proj.path("output", "timeline.json")
    write_json(timeline_path, {"project_id": proj.id, "video": str(final), "duration": round(total, 3),
                               "fps": str(fps), "script_key": script_key(beats), "beats": timeline_beats})
    actual = float((probe(final).get("format") or {}).get("duration") or 0)
    warnings = [f"Beat {p['beat']}: {w}" for p in plans for w in p["warnings"]]
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
        rows = [{"beat": b["beat"], "start": b["start"], "clips": b["clips"], "freeze": b["freeze_sec"], "notes": b["notes"]}
                for b in timeline["beats"]]
        source = "rendered video"
    else:
        timing, how = beat_rows(proj, beats)
        plans = plan_beats(proj, beats, [r["end"] - r["start"] for r in timing])
        rows = [{"beat": p["beat"], "start": r["start"], "clips": p["clips"], "freeze": p["freeze"],
                 "notes": p["notes"] + p["warnings"]}
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
