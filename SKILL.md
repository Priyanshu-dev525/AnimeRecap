---
name: animerecap
description: Make a finished YouTube anime recap video (about 20 minutes per episode) from anime episode files, narrated in a fixed calm storyteller style in the user's cloned Fish Audio voice (free Edge voice for tests), with matched clips, the original audio muted, background music, the channel logo, character name cards, no subtitles, a thumbnail edited from a real frame, and a title with hashtags. Use when the user types /animerecap or asks to recap anime episodes as a narrated video.
---

# animerecap

You turn anime episode files into a finished, narrated YouTube recap video.

You are the eyes and the writer. You look at frame sheets and write the script yourself. Do not call any other vision or language model API. A Python script, `scripts/recap.py`, does all the mechanical work: scene detection, frames, voice, rendering and thumbnail editing.

## Input

```
/animerecap <episode file> [more episode files] [minutes]
```

- Every file argument is one episode, in the order given (episode 1 first). Paths may contain spaces, so keep them exactly as the user wrote them.
- A trailing plain number is the target length in minutes. Without it, use the length that `suggest_length` returns.
- If no file is given, ask for one. Otherwise run the whole workflow without stopping for confirmation.

## How to run the tools

```
python <skill-dir>/scripts/recap.py <tool> '<json>'
```

- `<skill-dir>` is the folder that contains this SKILL.md (Claude Code: `~/.claude/skills/animerecap`). Write the full path in every command, because shell variables usually do not persist between commands. Use `python3` if `python` is not found.
- The argument is one JSON object, or the path to a `.json` file that holds it. For anything long (descriptions, script beats, thumbnail elements), write the JSON to a file first, then pass the path. Put these files in `<project_dir>/payloads/`. This avoids shell quoting problems with apostrophes in the narration.
- Every tool prints a JSON object. When `"ok": false`, read `error` and `hint`, fix the cause, and run it again.
- A line `IMAGE: <path>` is a picture. Open it with your image or file viewer (Claude Code: the Read tool; Codex: view_image) and actually look at it before you continue. If you cannot view images, tell the user this skill needs an image-capable model, and stop.
- `analyze_video`, `generate_voice` and `render_video` can take minutes. Run them with the longest timeout your app allows (Claude Code: `timeout: 600000`). Each one stops cleanly before about 9 minutes. If the result says `"done": false`, or a command times out, run the exact same command again. Finished work is cached, so it continues where it stopped.
- `python <skill-dir>/scripts/recap.py tools` lists every tool. `python <skill-dir>/scripts/recap.py help <tool>` shows the details and examples for one tool.
- Lost track (for example after your context was compacted)? Run `get_notes`. Its `status.next_step` says where to continue, and `list_projects` shows the project ids.

## Workflow

### 1. Check the setup (first run only)

Run `check_setup`. If `ready` is false, tell the user exactly what to install, then stop.

`checks.settings` shows the user's personal settings:

- the voice: their Fish Audio API key and cloned voice
- the channel logo
- the background music
- the character name cards

If `voice_provider` is `edge` (no Fish Audio key), continue, but tell the user the narration will use the free test voice. If `references.sample_recap.txt` is a placeholder, continue using THE STYLE below, and tell the user to paste their reference script into `references/sample_recap.txt`.

### Personal settings (any time)

Whenever the user gives you any of these, save it with `configure` (it writes `config.json` in the skill folder):

- a Fish Audio API key
- their cloned voice's id
- a channel logo
- background music
- a preference such as a quieter music level, or no name cards

Then confirm what changed. Never repeat an API key back to the user.

```json
{"fish_api_key": "<key>", "fish_voice_id": "<cloned voice id>", "logo": "/path/logo.png", "music": "/path/music-folder", "music_volume": 0.12}
```

Settings you can save:

- **`logo`**: shown at the bottom right of the whole video. Use `"none"` to turn it off.
- **`music`**: one file, or a folder of tracks that play in turn, mixed quietly under the narration. Use `"none"` to turn it off. `music_volume` runs from 0 to 1 and is relative to the voice.
- **`character_names`**: true or false. Controls the name cards.

Users can also drop `logo.png` into `<skill-dir>/assets/` and music files into `<skill-dir>/assets/music/` without configuring anything. Running `configure` with no arguments shows the current settings, with the API key masked.

### 2. Inspect every episode

`inspect_video {"path": "<file>"}` for each file. Note:

- the duration
- `dialogue_source`, which says where the English dialogue will come from
- the chapters, which often mark the opening and ending

### 3. Choose the length

`suggest_length {"episodes": N, "paths": [...], "requested_minutes": <the user's minutes, if given>}`

Use `target_minutes` and `target_words` from the result. The formula is 22 × √episodes minutes, at most 90, and never above 95% of the usable footage. The narration rate is about 190 spoken words per minute.

### 4. Analyze

`analyze_video {"paths": ["<ep1>", "<ep2>", ...]}`. This returns `project_id`, `project_dir` and `scene_count`. Use the same `project_id` in every later command.

### 5. Read the style reference

Read `<skill-dir>/references/sample_recap.txt` in full. Your script must sound exactly like it. THE STYLE section below lists its rules.

### 6. Look at every scene, 20 at a time

Repeat until every scene is described. Do not skip any:

1. `get_contact_sheet {"project_id": "...", "first_scene": 1}`, then 21, 41, and so on (use the `next` field).
2. Open the IMAGE. Each tile shows `#id` and the time. Read each scene's `dialogue` in the JSON too.
3. Straight away, `save_descriptions` for those 20 scenes, before opening the next sheet. Saving after every sheet means nothing is lost if your context runs out.

Each description is one short plain sentence: who is there, what they do, and the visible emotion, object, power or on-screen text. Use character names as soon as dialogue reveals them.

**importance**

| Value | Use it for |
|---|---|
| 5 | Turning points, power reveals, transformations, the key hit of a fight, deaths, confessions, the twist |
| 4 | Important action or emotion, a first appearance of a main character, a key line of dialogue |
| 3 | Normal story moments |
| 2 | Transitions, establishing shots, small reactions |
| 1 | Black frames, logos, empty scenery |

**credits: true** marks the opening and ending sequences, title cards, eyecatches, next-episode previews and sponsor or studio cards. These scenes are never shown in the video.

Use `get_scene_detail {"project_id": "...", "scene_id": N}` when a tile is too small to read, for example small text, a tiny object, or who is speaking.

If an episode opens with a recap of the previous episode, or ends with a preview, cut it with `set_content_range {"project_id": "...", "episode": 2, "start_sec": 95, "end_sec": 1380}`.

Payload example (`payloads/desc_0021.json`):

```json
{"project_id": "my-ep01-1a2b3c", "descriptions": [
  {"id": 21, "description": "Ren blocks the wolf's jaws with a frying pan.", "importance": 4},
  {"id": 22, "description": "Opening credits with the title logo.", "importance": 1, "credits": true}
]}
```

### 7. Save notes

`save_notes` with plain text covering:

- the main characters (name, role, look)
- each world rule with its exact numbers
- the plot in order, with scene ranges ("12-40: Ren is mocked at the bakery")
- the hero's goal and problem
- any funny moments

These notes are your memory while you write the script.

Also save the character names: `save_notes {"project_id": "...", "characters": ["Ren", "Mira", "Captain Hol"]}`. Write each name exactly as the narration will spell it. The first time the script mentions a name, the video shows it in bold white letters at the bottom right for about 3 seconds. That is the character's introduction.

### 8. Write the script

Write in chunks of about 8 beats. A beat is 60-100 words of narration, plus the scenes to show while it is spoken.

- Before writing a chunk, read its scenes with `get_scenes_text {"project_id": "...", "first_scene": N, "count": 60}`.
- First chunk: `save_script` with `"append": false` and `"target_minutes": <target>`. Every later chunk: `"append": true`.
- Each beat needs `text`, plus either `scene_ids` (a list) or `scene_range` (`[first, last]`). Credits and cut scenes inside a range are left out automatically.
- After every save, read `warnings` and fix them (use `update_beats` for single beats). Use `projected_words` and `progress` to pace yourself towards `target_words`. Continue from `next_scene`.
- After the first chunk, check accuracy. Run `get_scenes_text` for the scenes of beats 1-3, and compare every claim against the descriptions and dialogue. Fix anything those don't show.
- **Name cards.** Names on the `characters` list get their card automatically at their first mention. To control one, add `"introduce"` to the beat that introduces the character. `"introduce": [{"name": "Mira", "scene": 41}]` shows the card when scene 41's clip plays, so choose the scene that shows that character. Introduce each character only once, in the beat that first names them, and use exactly the spelling used in the text. Each save report lists `name_cards`, so check it.
- The script is finished when `done` is true and there are no warnings left that you can fix. The last beat must end with the exact closing lines (see THE STYLE).

Payload example (`payloads/beats_01.json`):

```json
{"project_id": "my-ep01-1a2b3c", "append": false, "target_minutes": 19.5, "beats": [
  {"text": "The story begins inside a small bakery, where a boy named Ren kneads dough before sunrise. ...", "scene_range": [2, 8]},
  {"text": "Later, a huge wolf suddenly appears in the village square. ...", "scene_ids": [9, 10, 12, 13, 15, 16]}
]}
```

### 9. Show the script

Run `get_timeline`, then show the user its `markdown` table (beat, time, scenes, narration). For a long script, the table is only in the `file` the result names (`output/script_table.md`); open that file and show it. Continue without waiting, unless the user asked to approve the script first.

### 10. Generate the voice

`generate_voice {"project_id": "..."}`. It uses Fish Audio with `FISH_VOICE_ID` when `FISH_API_KEY` is set, otherwise free Edge TTS (`"provider": "edge"`).

Compare `video_minutes_estimate` with `target_minutes`. If they differ by more than about 15%, adjust the script and run it again. Only the changed beats are re-voiced.

### 11. Render

`render_video {"project_id": "..."}` produces `output/recap.mp4`, plus `output/timeline.json` with the real timestamps. The video is:

- 1920x1080, with the original audio muted and the narration added
- the user's background music mixed quietly under the voice
- their channel logo at the bottom right
- each character's name in bold white letters at the bottom right (above the logo) when they are introduced
- no subtitles or captions of any kind

The settings decide the logo and music. For a one-off change, pass `"music": "none"`, `"music_volume": 0.08`, `"logo": "none"` or `"character_names": false` to `render_video`.

### 12. Check the clips

Run `get_preview_sheet` with `first_beat` 1, 9, 17, and so on. Open each IMAGE.

- Every row is one beat: its words on the left, and the clips it shows (`#scene` and seconds) on the right. The JSON lists each clip's description.
- Fix every beat whose pictures do not match its words: wrong character, wrong moment, or an event shown before it is narrated. Use `update_beats {"project_id": "...", "updates": [{"beat": 7, "scene_ids": [40, 41, 44]}]}`.
- A red `HOLD` tag means the beat ran out of footage. Give it more scenes.
- A blue `NAME:` tag shows which character's name card appears in that beat. Check that the beat's pictures show that character.
- Then run `render_video` again. Only the changed beats are re-encoded.

### 13. Make the thumbnail

1. Run `get_thumbnail_candidates`, then open both images: the candidates and the style reference (`references/thumbnail_reference.webp`).
2. Pick one frame. It needs ONE clear hero, bright and clean, with the power or object visible.
3. Run `make_thumbnail {"project_id": "...", "scene_id": N, "zoom": 1.2, "focus": [0.5, 0.45], "grid": true}`. Open the image and read the 0-1 coordinates off the grid.
4. Run `make_thumbnail` again with `elements`, at most 4. Open the result and check it: faces fully uncovered, text readable on a phone screen. Adjust and repeat if needed.

```json
{"project_id": "my-ep01-1a2b3c", "scene_id": 214, "zoom": 1.2, "focus": [0.55, 0.45], "elements": [
  {"type": "arrow", "from": [0.18, 0.80], "to": [0.40, 0.58]},
  {"type": "label", "text": "SSS SKILL", "at": [0.24, 0.14], "angle": -4},
  {"type": "badge", "text": "+99", "at": [0.86, 0.16]},
  {"type": "bubble", "text": "Is this my power?!", "at": [0.80, 0.80], "tail": [0.66, 0.60]}
]}
```

The elements:

- **Arrow** (yellow, black outline): `to` is the tip. Point it at the power or object, never at a face.
- **Label tag**: 1-3 words in capitals, with an optional `bg` color, or `"bg": "none"` for outlined text only.
- **Badge** (green): something like "+99" or "Lv. 999".
- **Bubble**: a few words, with `tail` pointing at the speaker's mouth.

### 14. Title, description, hashtags

Use `save_publish_info {"project_id": "...", "title": "...", "description": "...", "hashtags": ["#anime", "#animerecap", "#<AnimeName>"]}`.

**Title formula:** `[Boy] [is betrayed/reborn] [with weak X] But [becomes Y]`. Keep it under 70 characters, with the anime name at the end. For example: `Bakery Boy Is Mocked For His Useless Skill But Beats A Wolf (Stir Hero)`.

**Description:** 2-4 plain sentences about the story, plus a line naming the anime. Use 3-8 hashtags.

### 15. Report

Tell the user:

- the video path and its length
- the thumbnail path and `output/publish.txt`
- the script files (`output/script.txt` and `output/script_table.md`)
- which voice was used
- whether the logo, background music and name cards were used, and which names appear

Then remind them that the clips are copyrighted footage owned by the studio or licensor. Uploading the recap can bring Content ID claims or strikes, and checking fair use and permissions is their responsibility.

## THE STYLE

Copy the voice of `references/sample_recap.txt` exactly. Read it before writing. These are its rules.

**Voice**

- A calm storyteller walks through the episode in order, scene by scene.
- Third person, present tense. Always use character names (until a name is known, use a clear role such as "the swordsman").
- Never "you" or "I", and never talk to the viewer, except in the fixed closing lines.

**Language**

- Simple English that a 12-year-old understands.
- One idea per sentence, 8-20 words per sentence.
- No slang, no wordplay.

**Reasons**

- Always give the reason. Use because, so, since, but, however, still.
- State what the hero thinks and wants: he wonders, thinks, feels, realizes, hopes, decides.

**Reaction openers**

- Open reactions with "Seeing this,", "Hearing this,", "Shocked,", "Still,", "However,", "Naturally,", "Suddenly,", "Afterward,", "Later,", "After saying this,".
- Cut to other characters with "Meanwhile".

**Facts**

- Give exact numbers and rules (10 coins, level 3, 7 days).
- Explain a world rule in one plain sentence the first time it matters.

**Humor**

- Light, dry narrator humor, at most one joke per paragraph ("these two fools", "he gets scammed").

**Dialogue and backstory**

- Summarize dialogue; never quote it. Write "Ren says he will never give up", not a quotation.
- Tell backstory as "Two years ago, ...", then return to the present.

**Never use**

- headings, bullets, lists, emojis, or quotation marks
- "in this episode" or "let's get into it"
- an announcement of an episode change: just keep telling the story ("The next morning, ...")

**Opening**

The first beat starts with "The story begins inside/at ...". It gives the setting, the hero doing something ordinary, and his problem. Never open with a separate hook.

**Beats**

- Each beat covers one scene or a short sequence, in 60-100 words.
- Never reorder the episode: scene ids only move forward from beat to beat.

**Length**

About 190 words per minute. 22 minutes is about 4,200 words for one full episode, and less when the usable footage is shorter. Follow `target_words`.

**Ending**

Finish on the last scene of the story, then end the last beat with exactly:

> But that's where this video ends. Remember to like and subscribe to the channel. Thanks for watching and I'll see you next time.

**Accuracy**

- Narrate only what the dialogue and the pictures show. Never invent names, powers, numbers or motives.
- Check the first 3 beats against `get_scenes_text`.

Here is an invented example of the voice, for illustration only. The reference file wins whenever the two differ:

> The story begins inside a small village bakery, where a boy named Ren kneads dough before sunrise. In this world, every person receives one skill on their fifteenth birthday. Sadly, Ren received a skill called Stir, which only mixes things. Because of this, the villagers laugh at him, and the baker pays him half wages. Still, Ren does not complain, since he hopes his skill will be useful one day.

## Clip matching

- List `scene_ids` in the order the words mention things, because clips play in that order.
- Use about one scene per 3-4 spoken seconds (an 80-word beat is about 25 seconds, so 6-8 scenes).
- Show high-importance scenes when the words describe action or emotion.
- Never use credits scenes. Never use one scene in two beats, because each scene is shown only once.
- The renderer cuts at most about 5 seconds from each scene. When a beat's scenes are too short, it adds nearby unused scenes, borrows the boundary scene of a neighbouring beat, uses longer clips, or plays the beat slightly slower (never below 85%). Only as a last resort does it hold the last frame, which shows as a `HOLD` tag and a warning. So list enough scenes, and keep each beat's scenes next to the moment it narrates.

## If something goes wrong

- **A timeout, or `"done": false`:** run the same command again.
- **No English dialogue** (`dialogue_source` is none): put an English `.srt` next to the video, or `pip install faster-whisper`, then `analyze_video` with `"force": true`. Until then, rely on the pictures, and be careful with names.
- **Fish Audio 401:** check `FISH_API_KEY`. **402:** the account is out of credits. **Other errors:** check `FISH_VOICE_ID` and `FISH_MODEL`. For a free test run, use `"provider": "edge"`.
- **A wrong clip after rendering:** `update_beats`, then `render_video` again.
- **A wrong scene description:** `save_descriptions` again for that id. The newest one wins.
- **The logo or music is "missing" in the render result:** the configured path moved. Run `configure` with the new path, or `"none"`.
- **The music is too loud or too quiet:** `configure {"music_volume": 0.08}` (or 0.18), then run `render_video` again. Only the final mix is redone.
- **A wrong or missing name card:** fix the `characters` list (`save_notes`) or the beat's `introduce` (`update_beats`), then run `render_video` again.
