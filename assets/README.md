# Your channel assets

Drop your own files here. animerecap picks them up automatically, with no settings needed:

| File | What it does |
|---|---|
| `logo.png` (or `.webp`, `.jpg`) | Your channel logo, shown at the bottom right of every video. A PNG with a transparent background looks best. |
| `music/` | Background music (`.mp3`, `.m4a`, `.wav`, `.flac`, `.ogg`, `.opus`). The tracks play in turn under the narration, crossfading into each other and looping until the video ends. |

To use files somewhere else, ask the AI ("use ~/Pictures/logo.png as my channel logo"), or run:

```bash
python scripts/recap.py configure '{"logo": "/path/to/logo.png", "music": "/path/to/music-folder", "music_volume": 0.12}'
```

Everything in this folder except this README is git-ignored, so your logo and music are never committed by accident.

Only use music you have the rights to: royalty-free, or licensed for YouTube.
