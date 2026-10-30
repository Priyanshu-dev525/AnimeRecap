"""Script tools: save_script, update_beats, the script checks, get_timeline, save_publish_info."""
from __future__ import annotations

import json
import math
import re

from .common import (CLOSING_LINE, PAUSE_SEC, TAIL_SEC, WORDS_PER_MINUTE, Project, ToolError, compact_ids, fmt_time,
                     read_json, segment_durations, short_hash, speech_seconds, word_count, write_json)
from .config import load_settings
from .engine import target_minutes

MAX_CLIP_SEC = 5.0
MAX_WARNINGS = 40
PEOPLE_WORDS = {"you", "your", "yours", "yourself", "yourselves", "me", "my", "mine", "myself",
                "we", "us", "our", "ours", "ourselves"}
FIRST_PERSON_I = {"I", "I'm", "I've", "I'd", "I'll"}
BANNED_PHRASES = ("in this episode", "let's get into it", "let's", "let us", "without further ado",
                  "smash that", "hey guys")
EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF]")


def _scenes(ids) -> str:
    """'scene 4' or 'scenes 4-6, 9'."""
    ids = list(ids)
    return f"scene {ids[0]}" if len(ids) == 1 else f"scenes {compact_ids(ids)}"


def script_key(beats: list[dict]) -> str:
    return short_hash(json.dumps([[b["text"], b["scene_ids"], b.get("introduce") or []] for b in beats]))


def name_pattern(name: str) -> re.Pattern:
    return re.compile(r"(?<!\w)" + re.escape(name) + r"(?!\w)")


def resolve_introduce(beat: dict, number: int) -> list[dict]:
    """introduce: ["Ren"] or [{"name": "Ren", "scene": 14}] (the scene times the card to that clip)."""
    raw = beat.get("introduce")
    if raw is None:
        return []
    out = []
    for item in raw if isinstance(raw, list) else [raw]:
        if isinstance(item, str):
            name, scene = item, None
        elif isinstance(item, dict):
            name, scene = item.get("name"), item.get("scene")
        else:
            raise ToolError(f"Beat {number}: introduce entries are names or {{\"name\": ..., \"scene\": ...}}.")
        name = " ".join(str(name or "").split())
        if not name or len(name) > 40:
            raise ToolError(f"Beat {number}: every introduced character needs a short name (max 40 characters).")
        if scene is not None:
            try:
                scene = int(scene)
            except (TypeError, ValueError):
                raise ToolError(f"Beat {number}: the scene for {name}'s name card must be a scene id.")
        out.append({"name": name, "scene": scene})
    return out


def name_card_plan(proj: Project, beats: list[dict]) -> dict[int, list[dict]]:
    """Beat index -> the names shown there: each character once, the first time they are introduced.

    Explicit `introduce` entries win; every other name on the project's characters list gets its
    card in the first beat whose text mentions it.
    """
    plan: dict[int, list[dict]] = {}
    shown: set[str] = set()
    for i, beat in enumerate(beats):
        for item in beat.get("introduce") or []:
            if item["name"].lower() not in shown:
                shown.add(item["name"].lower())
                plan.setdefault(i, []).append(item)
    for name in proj.meta.get("characters") or []:
        if name.lower() in shown:
            continue
        pattern = name_pattern(name)
        for i, beat in enumerate(beats):
            if pattern.search(beat["text"]):
                plan.setdefault(i, []).append({"name": name, "scene": None})
                shown.add(name.lower())
                break
    return plan


def resolve_scene_ids(proj: Project, beat: dict, number: int) -> list[int]:
    """scene_ids as given (order kept) plus any scene_range [a, b] (inclusive, credits/cut scenes left out)."""
    ids: list[int] = []
    raw = beat.get("scene_ids", beat.get("scenes"))
    if raw is not None:
        for value in (raw if isinstance(raw, list) else [raw]):
            try:
                ids.append(int(value))
            except (TypeError, ValueError):
                raise ToolError(f"Beat {number}: scene id '{value}' is not a number.")
    rng = beat.get("scene_range")
    if rng is not None:
        if not (isinstance(rng, list) and len(rng) == 2):
            raise ToolError(f"Beat {number}: scene_range must be [first, last].")
        a, b = sorted(int(v) for v in rng)
        ids.extend(i for i in range(a, b + 1) if proj.get_scene(i) and proj.usable(proj.get_scene(i)))
    if raw is None and rng is None:
        raise ToolError(f"Beat {number} has no scene_ids or scene_range.",
                        hint="Every beat needs the scenes to show while it is spoken, in the order the words mention them.")
    seen: set[int] = set()
    return [i for i in ids if not (i in seen or seen.add(i))]


def _body(text: str) -> str:
    """Beat text without the fixed closing line (which is allowed to say 'I')."""
    return text[: -len(CLOSING_LINE)].rstrip() if text.endswith(CLOSING_LINE) else text


def style_issues(text: str, first: bool) -> list[str]:
    body = _body(text).replace("’", "'").replace("‘", "'")
    issues = []
    if first and not body.lstrip().startswith("The story begins"):
        issues.append('the first beat must start with "The story begins"')
    if re.search('["“”]', body):
        issues.append("has quotation marks; summarize dialogue instead of quoting it")
    tokens = re.findall(r"[A-Za-z']+", body)
    personal = sorted({t for t in tokens if t.lower() in PEOPLE_WORDS or t in FIRST_PERSON_I})
    if personal:
        issues.append(f"uses {', '.join(personal)}; narrate in third person (never 'you' or 'I')")
    lower = body.lower()
    found: list[str] = []
    for phrase in BANNED_PHRASES:
        if not any(phrase in f for f in found) and re.search(r"(^|\W)" + re.escape(phrase) + r"(\W|$)", lower):
            found.append(phrase)
            issues.append(f'contains "{phrase}"')
    if EMOJI_RE.search(body):
        issues.append("contains emoji")
    if re.match(r"\s*([#*\-•]|\d+[.)])\s", body):
        issues.append("starts like a heading or bullet")
    long_sentences = [s for s in re.split(r"(?<=[.!?])\s+", body) if len(s.split()) > 25]
    if long_sentences:
        issues.append(f"{len(long_sentences)} sentence(s) over 25 words; keep sentences 8-20 words")
    return issues


def check_script(proj: Project, script: dict) -> dict:
    beats = script["beats"]
    usable_list = [s for s in proj.scenes if proj.usable(s)]
    total_footage = sum(s["end"] - s["start"] for s in usable_list)
    target = target_minutes(proj, script)
    target_words = int(round(target * WORDS_PER_MINUTE))
    total_words = sum(word_count(b["text"]) for b in beats)
    warnings: list[str] = []
    owner: dict[int, int] = {}
    reached = 0
    for n, beat in enumerate(beats, 1):
        words = word_count(beat["text"])
        if beat["text"] != CLOSING_LINE:
            if words < 50:
                warnings.append(f"Beat {n}: only {words} words; beats are 60-100 words.")
            elif words > 115:
                warnings.append(f"Beat {n}: {words} words; split it into two beats (60-100 words each).")
        ids = beat["scene_ids"]
        missing = [i for i in ids if proj.get_scene(i) is None]
        if missing:
            warnings.append(f"Beat {n}: {_scenes(missing)} not found (scenes are 1-{len(proj.scenes)}).")
        unusable = [i for i in ids if proj.get_scene(i) and not proj.usable(proj.get_scene(i))]
        if unusable:
            warnings.append(f"Beat {n}: {_scenes(unusable)} (credits or cut) will not be shown.")
        reused = [i for i in ids if i in owner]
        if reused:
            earlier = compact_ids({owner[i] for i in reused})
            warnings.append(f"Beat {n}: {_scenes(reused)} already listed in beat(s) {earlier}; "
                            "each scene is shown only once, so the repeat is skipped.")
        valid = [i for i in ids if proj.get_scene(i) and proj.usable(proj.get_scene(i)) and i not in owner]
        for i in valid:
            owner[i] = n
        if not valid:
            warnings.append(f"Beat {n}: no usable scenes; add scene_ids.")
        else:
            if min(valid) < reached:
                warnings.append(f"Beat {n}: goes back to scene {min(valid)} after the script reached scene {reached}. "
                                "Never reorder the episode.")
            reached = max(reached, max(valid))
            need = speech_seconds(word_count(beat["text"]))
            have = sum(min(proj.scene(i)["end"] - proj.scene(i)["start"] - 0.12, MAX_CLIP_SEC) for i in valid)
            if have < 0.7 * need:
                more = math.ceil((need - have) / 3.5)
                warnings.append(f"Beat {n}: ~{need:.0f}s of narration but only ~{have:.0f}s of footage in "
                                f"{len(valid)} scene(s); add about {more} more scenes (one per 3-4 spoken seconds).")
        warnings.extend(f"Beat {n} style: {issue}." for issue in style_issues(beat["text"], first=n == 1))

    first_named: dict[str, int] = {}
    for n, beat in enumerate(beats, 1):
        for item in beat.get("introduce") or []:
            key = item["name"].lower()
            if key in first_named:
                warnings.append(f"Beat {n}: {item['name']} was already introduced in beat {first_named[key]}; "
                                "the name card shows only the first time.")
                continue
            first_named[key] = n
            if not name_pattern(item["name"]).search(beat["text"]):
                warnings.append(f"Beat {n}: the name {item['name']} is not in this beat's text, "
                                "so its card shows near the start of the beat.")
            if item["scene"] is not None and item["scene"] not in beat["scene_ids"]:
                warnings.append(f"Beat {n}: scene {item['scene']} for {item['name']}'s name card is not one of this beat's scenes.")
            earlier = next((m for m, b in enumerate(beats[:n - 1], 1) if name_pattern(item["name"]).search(b["text"])), None)
            if earlier:
                warnings.append(f"Beat {n}: {item['name']} is already named in beat {earlier}; introduce a character "
                                "where the story first names them.")

    # Parts of the story the script walked past without showing or telling.
    run: list[dict] = []

    def flush_gap():
        seconds = sum(s["end"] - s["start"] for s in run)
        if run and seconds > 45:
            warnings.append(f"Scenes {run[0]['id']}-{run[-1]['id']} ({seconds:.0f}s of footage) are skipped; "
                            "did the script jump over part of the story?")
        run.clear()

    for s in usable_list:
        if s["id"] > reached:
            break
        if s["id"] in owner:
            flush_gap()
        else:
            run.append(s)
    flush_gap()
    key_skipped = [s["id"] for s in usable_list if s["id"] < reached and s["id"] not in owner and (s.get("importance") or 0) >= 5]
    if key_skipped:
        warnings.append(f"Importance-5 scenes not shown: {compact_ids(key_skipped)}; check that the script covers them.")

    remaining = [s for s in usable_list if s["id"] > reached]
    remaining_seconds = sum(s["end"] - s["start"] for s in remaining)
    done = bool(beats) and (remaining_seconds < 30 or beats[-1]["text"].endswith(CLOSING_LINE))
    est_seconds = speech_seconds(total_words) + len(beats) * PAUSE_SEC + (TAIL_SEC - PAUSE_SEC if beats else 0)
    report: dict = {
        "beats": len(beats),
        "words": total_words,
        "estimated_minutes": round(est_seconds / 60, 1),
        "target_minutes": target,
        "target_words": target_words,
        "progress": f"{round(100 * total_words / target_words) if target_words else 0}% of target words",
        "reached_scene": reached,
        "next_scene": remaining[0]["id"] if remaining else None,
        "done": done,
    }
    if load_settings()["character_names"]:
        report["name_cards"] = [f"{item['name']} (beat {i + 1})"
                                for i, items in sorted(name_card_plan(proj, beats).items()) for item in items]
    else:
        report["name_cards"] = "off (character_names is false)"
    if done:
        if total_words < 0.85 * target_words:
            warnings.append(f"Too short: {total_words} words (~{est_seconds / 60:.1f} min) for a {target:g}-minute "
                            f"target ({target_words} words). Add detail to beats or cover skipped scenes.")
        elif total_words > 1.12 * target_words:
            warnings.append(f"Too long: {total_words} words (~{est_seconds / 60:.1f} min) for a {target:g}-minute "
                            f"target ({target_words} words). Trim the weakest beats.")
        if not beats[-1]["text"].endswith(CLOSING_LINE):
            warnings.append(f'The last beat must end with exactly: "{CLOSING_LINE}"')
    elif beats:
        covered = total_footage - remaining_seconds
        if covered > 0:
            projected = int(total_words * total_footage / covered)
            report["projected_words"] = projected
            if projected < 0.85 * target_words:
                warnings.append(f"At this pace the script ends near {projected} words; the target is {target_words}. "
                                "Write a little more per scene.")
            elif projected > 1.12 * target_words:
                warnings.append(f"At this pace the script ends near {projected} words; the target is {target_words}. "
                                "Write a little less per scene.")
    if est_seconds > 0.97 * total_footage:
        warnings.append(f"Not enough footage: ~{est_seconds / 60:.1f} min of narration but only "
                        f"{total_footage / 60:.1f} min of usable footage. Shorten the script.")
    if len(warnings) > MAX_WARNINGS:
        warnings = warnings[:MAX_WARNINGS] + [f"... and {len(warnings) - MAX_WARNINGS} more warnings"]
    report["warnings"] = warnings
    if not done:
        nxt = report["next_scene"]
        report["next"] = f"Continue the script from scene {nxt} (save_script with append=true)." if nxt else "Finish the script."
    return report


def _write_script_text(proj: Project, beats: list[dict]) -> None:
    out = proj.path("output", "script.txt")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n\n".join(b["text"] for b in beats) + "\n", encoding="utf-8")


def save_script(args: dict) -> dict:
    proj = Project.from_args(args)
    beats_in = args.get("beats")
    if not isinstance(beats_in, list) or not beats_in:
        raise ToolError("beats must be a non-empty list of {text, scene_ids | scene_range}.",
                        hint='Example: {"project_id": "...", "beats": [{"text": "The story begins ...", "scene_range": [3, 9]}]}')
    old = proj.load_script()
    append = bool(args.get("append"))
    script = old if append else {"beats": [], **({"target_minutes": old["target_minutes"]} if old.get("target_minutes") else {})}
    first = len(script["beats"]) + 1
    for i, beat in enumerate(beats_in):
        number = first + i
        if not isinstance(beat, dict):
            raise ToolError(f"Beat {number} must be an object with text and scene_ids.")
        text = " ".join(str(beat.get("text") or "").split())
        if not text:
            raise ToolError(f"Beat {number} has no text.")
        entry = {"text": text, "scene_ids": resolve_scene_ids(proj, beat, number)}
        names = resolve_introduce(beat, number)
        if names:
            entry["introduce"] = names
        script["beats"].append(entry)
    if args.get("target_minutes"):
        script["target_minutes"] = float(args["target_minutes"])
    proj.save_script(script)
    _write_script_text(proj, script["beats"])
    return {"saved_beats": f"{first}-{len(script['beats'])}", **check_script(proj, script)}


def update_beats(args: dict) -> dict:
    proj = Project.from_args(args)
    script = proj.load_script()
    beats = script["beats"]
    updates = args.get("updates")
    if not isinstance(updates, list) or not updates:
        raise ToolError("updates must be a non-empty list of {beat, text?, scene_ids? | scene_range?, introduce?, delete?}.")
    changed, deleted = [], []
    for update in updates:
        try:
            number = int(update.get("beat"))
        except (TypeError, ValueError, AttributeError):
            raise ToolError("Each update needs a beat number (1-based).")
        if not 1 <= number <= len(beats):
            raise ToolError(f"Beat {number} does not exist (the script has {len(beats)} beats).")
        if update.get("delete"):
            deleted.append(number)
            continue
        beat = beats[number - 1]
        if update.get("text"):
            beat["text"] = " ".join(str(update["text"]).split())
        if any(k in update for k in ("scene_ids", "scenes", "scene_range")):
            beat["scene_ids"] = resolve_scene_ids(proj, update, number)
        if "introduce" in update:
            names = resolve_introduce(update, number)
            if names:
                beat["introduce"] = names
            else:
                beat.pop("introduce", None)
        changed.append(number)
    for number in sorted(set(deleted), reverse=True):
        beats.pop(number - 1)
    proj.save_script(script)
    _write_script_text(proj, beats)
    return {"updated_beats": changed, "deleted_beats": sorted(set(deleted)), **check_script(proj, script)}


def beat_rows(proj: Project, beats: list[dict]) -> tuple[list[dict], str]:
    """[{beat, start, end}] from the rendered timeline, else voice durations, else 190 words per minute."""
    from .voice import voice_durations  # local import: voice imports nothing from here, keep it that way

    timeline = read_json(proj.path("output", "timeline.json"), None)
    if timeline and timeline.get("script_key") == script_key(beats):
        return [{"beat": b["beat"], "start": b["start"], "end": b["end"]} for b in timeline["beats"]], "rendered video"
    spoken = voice_durations(proj, beats)
    source = "voice durations"
    if any(d is None for d in spoken):
        spoken = [d if d is not None else speech_seconds(word_count(b["text"])) for d, b in zip(spoken, beats)]
        source = "estimate (190 words per minute)"
    rows, t = [], 0.0
    for n, seconds in enumerate(segment_durations(spoken), 1):
        rows.append({"beat": n, "start": t, "end": t + seconds})
        t += seconds
    return rows, source


def get_timeline(args: dict) -> dict:
    proj = Project.from_args(args)
    beats = proj.load_script()["beats"]
    if not beats:
        raise ToolError("There is no script yet.", hint="Write it with save_script first.")
    rows, source = beat_rows(proj, beats)
    table = ["| # | Time | Scenes | Narration |", "|---|---|---|---|"]
    out = []
    for row, beat in zip(rows, beats):
        scenes = compact_ids(beat["scene_ids"])
        table.append(f"| {row['beat']} | {fmt_time(row['start'])} | {scenes} | {beat['text'].replace('|', '/')} |")
        out.append({"beat": row["beat"], "start": fmt_time(row["start"]), "end": fmt_time(row["end"]),
                    "start_sec": round(row["start"], 2), "scenes": scenes, "words": word_count(beat["text"])})
    markdown = "\n".join(table)
    path = proj.path("output", "script_table.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown + "\n", encoding="utf-8")
    result = {"source": source, "estimated": source != "rendered video", "total": fmt_time(rows[-1]["end"]),
              "beats": out, "file": str(path)}
    if len(markdown) <= 12000:
        result["markdown"] = markdown
    else:  # host apps truncate long command output; the file holds the whole table
        result["note"] = f"The table is long; open {path} to show it."
    return result


def save_publish_info(args: dict) -> dict:
    proj = Project.from_args(args)
    title = " ".join(str(args.get("title") or "").split())
    description = str(args.get("description") or "").strip()
    raw_tags = args.get("hashtags") or []
    if isinstance(raw_tags, str):
        raw_tags = re.split(r"[\s,]+", raw_tags)
    if not title:
        raise ToolError("title is required.")
    tags, seen = [], set()
    for tag in raw_tags:
        clean = "#" + re.sub(r"[^\w]", "", str(tag).lstrip("#"))
        if len(clean) > 1 and clean.lower() not in seen:
            seen.add(clean.lower())
            tags.append(clean)
    warnings = []
    if len(title) > 70:
        warnings.append(f"Title is {len(title)} characters; keep it under 70.")
    if " but " not in f" {title.lower()} ":
        warnings.append('Title formula: "[Boy] [is betrayed/reborn] [with weak X] But [becomes Y]" with the anime name at the end.')
    if not 3 <= len(tags) <= 15:
        warnings.append("Use 3-15 hashtags (YouTube shows the first 3 above the title).")
    data = {"title": title, "description": description, "hashtags": tags}
    write_json(proj.path("output", "publish.json"), data)
    text = f"TITLE\n{title}\n\nDESCRIPTION (paste as is; the hashtags are at the end)\n{description}\n\n{' '.join(tags)}\n"
    proj.path("output", "publish.txt").write_text(text, encoding="utf-8")
    return {"title_length": len(title), "hashtags": tags, "warnings": warnings,
            "files": [str(proj.path("output", "publish.txt")), str(proj.path("output", "publish.json"))]}
