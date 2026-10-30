# animerecap

An AI agent skill that turns anime episode files into a finished YouTube recap video of about 20 minutes. It works in any coding AI app: Claude Code, Codex, Gemini CLI, OpenCode or Cursor.

```
/animerecap ~/Videos/ep01.mkv ~/Videos/ep02.mkv 25
```

From that one command, the AI app produces:

- **`recap.mp4`**: 1920x1080, with clips matched to the narration, the original audio muted, and a voice-over in your cloned Fish Audio voice. It is written in a fixed, calm storyteller style.
- **`thumbnail.jpg`**: 1280x720, edited from a real frame of the episode. It adds a thick yellow arrow, a bold label, an optional green "+99" badge and an optional speech bubble.
- **`publish.txt`**: the title, description and hashtags.
- **`script.txt`, `script_table.md` and `timeline.json`**: the narration, with real timestamps.

There is no website and no separate vision API. The AI in your app looks at frame sheets and writes the script itself. A Python script (`scripts/recap.py`) does all the mechanical work: scene detection, frames, voice, rendering and thumbnail editing.

## How it works

```
inspect_video → suggest_length → analyze_video → contact sheets + descriptions (20 scenes at a time)
→ notes → script in chunks (checked for style, pacing, order and footage) → timestamped script table
→ generate_voice → render_video → preview sheets + fixes → thumbnail → title/hashtags
```

`SKILL.md` holds the full instructions the AI follows, including the narration style rules.

## Requirements

- Python 3.10+
- ffmpeg and ffprobe on your `PATH` (`brew install ffmpeg`, `sudo apt install ffmpeg`, or `winget install ffmpeg`)
- `pip install -r requirements.txt` (Pillow and edge-tts)
- An AI app with a model that can view images
- For the real voice, a [Fish Audio](https://fish.audio) API key and a cloned voice. Edge TTS is free and works for testing.

## Install

### Claude Code

```bash
git clone https://github.com/Priyanshu-dev525/AnimeRecap ~/.claude/skills/animerecap
```

```bash
pip install -r ~/.claude/skills/animerecap/requirements.txt
```

Or copy this folder to `~/.claude/skills/animerecap` yourself. Then set the environment variables (see Configuration) and restart Claude Code. Run it like this:

```
/animerecap "/path/to/episode 01.mkv"
```

You can also just ask: "make an anime recap of ~/Videos/ep01.mkv".

### Other AI apps (Codex, Gemini CLI, OpenCode, Cursor, ...)

1. Clone the repo anywhere, for example `~/animerecap`, then install the requirements:

   ```bash
   git clone https://github.com/Priyanshu-dev525/AnimeRecap ~/animerecap
   ```

   ```bash
   pip install -r ~/animerecap/requirements.txt
   ```

2. Paste the contents of `SKILL.md` into the app's agent or system prompt. That is `AGENTS.md` for Codex and OpenCode, `GEMINI.md` for Gemini CLI, or a rule in `.cursor/rules/` for Cursor.
3. Replace `<skill-dir>` in the pasted text with the clone path (`~/animerecap`).
4. Ask: `/animerecap /path/to/ep01.mkv`, or "recap this anime episode: /path/to/ep01.mkv".

If your app sandboxes the filesystem, set `RECAP_HOME` to a folder it is allowed to write to. Without it, projects go to `~/animerecap_projects`, or to `./animerecap_projects` when the home folder is not writable.

## Configuration

| Variable | Required | Meaning |
|---|---|---|
| `FISH_API_KEY` | for the real voice | Your Fish Audio API key |
| `FISH_VOICE_ID` | for the real voice | The `reference_id` of your cloned Fish Audio voice |
| `FISH_MODEL` | no | Sent as the `model` header (default `s1`); use the model name from Fish Audio's docs |
| `EDGE_VOICE` | no | The Edge TTS voice for free test runs (default `en-US-GuyNeural`) |
| `RECAP_HOME` | no | The folder where projects are stored (default `~/animerecap_projects`) |
| `FISH_API_URL` | no | Another endpoint, for example a self-hosted Fish Speech server (default `https://api.fish.audio/v1/tts`) |
| `WHISPER_MODEL` | no | The Whisper model to use when an episode has no English subtitles (default `small`) |
| `RECAP_FONT` | no | A heavy `.ttf` or `.otf` for thumbnail text; you can also drop a font into `scripts/recap/fonts/` |
| `RECAP_TIME_BUDGET` | no | The seconds a long tool runs before stopping cleanly to resume later (default `540`) |

For example, on macOS or Linux add these lines to `~/.zshrc` or `~/.bashrc`:

```bash
export FISH_API_KEY="your-key"
export FISH_VOICE_ID="your-voice-reference-id"
```

The API key is only read from the environment. Never commit it.

## Subtitles

For the script, the AI needs the English dialogue. `analyze_video` looks for it in this order:

1. An English sidecar file next to the video, such as `ep01.srt`, `ep01.en.srt` or `ep01.eng.ass`.
2. The best embedded English text track. Full dialogue tracks win over "Signs & Songs" tracks. Karaoke, sign and song lines are dropped.
3. Optionally, Whisper (`pip install faster-whisper`), which translates Japanese audio to English.

Image-based subtitles (PGS or VobSub) cannot be read. Without any dialogue, the AI works from the pictures alone.

## Using the tools by hand

Every step is a plain command that prints JSON:

```bash
python scripts/recap.py tools                      # list every tool
```

```bash
python scripts/recap.py help make_thumbnail        # details and examples for one tool
```

```bash
python scripts/recap.py check_setup
```

```bash
python scripts/recap.py inspect_video '{"path": "/videos/ep01.mkv"}'
```

```bash
python scripts/recap.py analyze_video '{"paths": ["/videos/ep01.mkv"]}'
```

```bash
python scripts/recap.py get_contact_sheet '{"project_id": "ep01-1a2b3c", "first_scene": 1}'
```

```bash
python scripts/recap.py save_script /path/to/beats.json   # a JSON file works anywhere JSON does
```

The argument can be:

- a JSON object
- a path to a `.json` file
- `key=value` pairs (a value may be JSON, or `@file.json`)
- `-` to read the JSON from stdin

Tools that produce a picture also print `IMAGE: <path>`.

| Tool | What it does |
|---|---|
| `inspect_video` | Duration, audio tracks, subtitle tracks and chapters (ffprobe) |
| `suggest_length` | 22 × √episodes minutes, at most 90, never above 95% of the usable footage; about 190 words per minute |
| `analyze_video` | Scene detection (`select='gt(scene,0.3)'`): shots over 10 s are split, shots under 1 s merged. One keyframe per scene, plus the English dialogue |
| `get_contact_sheet` / `get_scene_detail` | A grid of keyframes, each tile labelled with its scene id and time, or one large frame |
| `save_descriptions` | Saves what each scene shows, with an importance of 1-5. `credits=true` keeps OP/ED and title cards out of the video |
| `set_content_range` | Uses only part of an episode, to cut "previously on" recaps or previews |
| `save_notes` / `get_notes` / `get_scenes_text` | Story notes, a progress status, and the scenes as text |
| `save_script` / `update_beats` | Saves narration beats. Checks word counts, pacing, style, scene order, skipped scenes and footage |
| `generate_voice` | One mp3 per beat (Fish Audio or Edge), cached by text |
| `render_video` | Cuts clips per beat (at most about 5 s each, never reusing a scene). A beat short of footage borrows nearby scenes or plays up to 15% slower. Mutes the original audio, adds the narration, and writes `timeline.json` |
| `get_preview_sheet` / `get_timeline` | Shows what each beat displays, and the real MM:SS timestamps |
| `get_thumbnail_candidates` / `make_thumbnail` | The best frames, then the thumbnail: arrow, label, badge and bubble on a real frame |
| `save_publish_info` | Saves the title, description and hashtags |
| `list_projects` / `check_setup` | Existing projects, and an environment check |

Long tools (`analyze_video`, `generate_voice`, `render_video`) cache everything they finish. If one is interrupted or times out, run the same command again and it continues. Changing one beat later re-voices and re-renders only that beat.

## Project files

Each project lives in `RECAP_HOME/<project_id>/`. The deliverables are in `output/`:

```
output/recap.mp4          the video
output/thumbnail.jpg      1280x720 thumbnail
output/publish.txt        title, description, hashtags
output/script.txt         narration text
output/script_table.md    timestamped script table
output/timeline.json      every beat and clip with real timestamps
```

## Style references

- **`references/sample_recap.txt`**: the reference narration script. The AI reads it before writing and copies its voice. Paste your own script here.
- **`references/thumbnail_reference.webp`**: the thumbnail style to match. Add your own image here.

## A free test run

Without a Fish Audio key, `generate_voice` uses Edge TTS. You can also ask for it explicitly:

```bash
python scripts/recap.py generate_voice '{"project_id": "<id>", "provider": "edge"}'
```

## Copyright

The video clips in a recap are copyrighted footage owned by the anime's studio or licensor. Uploading recaps can bring Content ID claims, demonetization or strikes. Make sure your use is allowed (for example, fair use commentary in your country) before you publish. This tool never generates new art: thumbnails are edited real frames.

## License

MIT. See [LICENSE](LICENSE).
