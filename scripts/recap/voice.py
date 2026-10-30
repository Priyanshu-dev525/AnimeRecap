"""Narration: one mp3 per beat with Fish Audio (your cloned voice) or Edge TTS (free, for testing).

Every beat's audio is cached by (provider, voice, model, text), so editing one beat
only re-voices that beat.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import wave
from array import array
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .common import PAUSE_SEC, Budget, Project, ToolError, log, short_hash, word_count, write_json
from .config import load_settings
from .engine import target_minutes
from .media import run

FISH_URL = "https://api.fish.audio/v1/tts"
SAMPLE_RATE = 48000


def _key(provider: str, voice_id: str, model: str, text: str) -> str:
    return short_hash(provider, voice_id, model, text)


def voice_durations(proj: Project, beats: list[dict]) -> list[float | None]:
    """Narration length of each beat, or None where the audio is missing or out of date."""
    voice = proj.load_voice()
    entries = {e["beat"]: e for e in voice.get("beats", [])}
    out: list[float | None] = []
    for n, beat in enumerate(beats, 1):
        e = entries.get(n)
        current = (e and e.get("duration")
                   and e.get("key") == _key(voice.get("provider"), voice.get("voice_id"), voice.get("model"), beat["text"])
                   and proj.path("voice", e["wav"]).exists())
        out.append(e["duration"] if current else None)
    return out


def voice_wavs(proj: Project) -> dict[int, Path]:
    return {e["beat"]: proj.path("voice", e["wav"]) for e in proj.load_voice().get("beats", [])}


# ---------------------------------------------------------------- providers

def fish_tts(text: str, voice_id: str, model: str, key: str, out: Path) -> None:
    body = json.dumps({"text": text, "reference_id": voice_id, "format": "mp3"}).encode("utf-8")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json", "model": model}
    url = os.environ.get("FISH_API_URL") or FISH_URL
    for attempt in range(4):
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                data = response.read()
            if not data or data[:1] in (b"{", b"<"):
                raise ToolError(f"Fish Audio returned no audio: {data[:200].decode('utf-8', 'replace')}")
            out.write_bytes(data)
            return
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:300].decode("utf-8", "replace")
            if exc.code in (401, 403):
                raise ToolError(f"Fish Audio rejected the request (HTTP {exc.code}): {detail}",
                                hint="Check your Fish Audio API key (configure fish_api_key, or FISH_API_KEY).")
            if exc.code == 402:
                raise ToolError("Fish Audio: out of credits (HTTP 402).",
                                hint="Top up your Fish Audio balance, or use provider 'edge' for a free test voice.")
            if exc.code in (408, 429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(2 ** attempt * 2)
                continue
            raise ToolError(f"Fish Audio error (HTTP {exc.code}): {detail}",
                            hint="Check your cloned voice id (fish_voice_id) and fish_model.")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            if attempt < 3:
                time.sleep(2 ** attempt * 2)
                continue
            raise ToolError(f"Could not reach Fish Audio: {exc}", hint="Check the internet connection.")


def edge_tts(text: str, voice: str, out: Path) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write(text)
        text_file = f.name
    try:
        proc = subprocess.run([sys.executable, "-m", "edge_tts", "--voice", voice, "--file", text_file,
                               "--write-media", str(out)], capture_output=True, timeout=300)
    except subprocess.TimeoutExpired:
        raise ToolError("edge-tts timed out.", hint="Check the internet connection and retry.")
    finally:
        os.unlink(text_file)
    if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        err = proc.stderr.decode("utf-8", "replace").strip().splitlines()[-3:]
        raise ToolError(f"edge-tts failed: {' | '.join(err)}",
                        hint="Check the internet connection and the voice name (python -m edge_tts --list-voices).")


# ---------------------------------------------------------------- audio files

def _read_mono16(path: Path) -> array:
    with wave.open(str(path), "rb") as r:
        if r.getnchannels() != 1 or r.getsampwidth() != 2:
            raise ToolError(f"Unexpected WAV format in {path.name}.")
        samples = array("h")
        samples.frombytes(r.readframes(r.getnframes()))
    if sys.byteorder == "big":
        samples.byteswap()
    return samples


def write_mono16(path: Path, samples: array) -> None:
    if sys.byteorder == "big":
        samples = array("h", samples)
        samples.byteswap()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(samples.tobytes())


def to_trimmed_wav(mp3: Path, wav: Path, threshold_db: float = -45.0, keep_sec: float = 0.06) -> float:
    """Decode to 48 kHz mono and trim leading/trailing silence so pacing is even. Returns seconds."""
    raw = wav.with_name(wav.stem + ".raw.wav")
    run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", mp3, "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", raw])
    samples = _read_mono16(raw)
    raw.unlink()
    limit = int(32768 * 10 ** (threshold_db / 20))
    window = SAMPLE_RATE // 100

    def loud(i: int) -> bool:
        chunk = samples[i:i + window]
        return bool(chunk) and max(max(chunk), -min(chunk)) > limit

    start, end = 0, len(samples)
    while start < end and not loud(start):
        start += window
    while end > start and not loud(max(start, end - window)):
        end -= window
    pad = int(keep_sec * SAMPLE_RATE)
    start, end = max(0, start - pad), min(len(samples), end + pad)
    if end <= start:
        start, end = 0, len(samples)
    write_mono16(wav, samples[start:end])
    return (end - start) / SAMPLE_RATE


def wav_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as r:
        return r.getnframes() / float(r.getframerate())


# ---------------------------------------------------------------- tool

def generate_voice(args: dict) -> dict:
    proj = Project.from_args(args)
    script = proj.load_script()
    beats = script["beats"]
    if not beats:
        raise ToolError("There is no script yet.", hint="Write it with save_script first.")
    settings = load_settings()
    api_key = settings["fish_api_key"]
    has_fish = bool(api_key and (args.get("voice_id") or settings["fish_voice_id"]))
    provider = str(args.get("provider") or ("fish" if has_fish else "edge")).lower()
    if provider == "fish":
        voice_id = args.get("voice_id") or settings["fish_voice_id"]
        model = settings["fish_model"] or "s1"
        if not api_key:
            raise ToolError("No Fish Audio API key.",
                            hint="Run configure with fish_api_key (from fish.audio), or set FISH_API_KEY. "
                                 "Use provider 'edge' for a free test voice.")
        if not voice_id:
            raise ToolError("No cloned voice.", hint="Run configure with fish_voice_id (your voice's id on fish.audio), "
                                                     "pass voice_id, or set FISH_VOICE_ID.")

        def synth(text: str, out: Path) -> None:
            fish_tts(text, voice_id, model, api_key, out)
    elif provider == "edge":
        voice_id = args.get("voice_id") or settings["edge_voice"]
        model = "edge"
        if importlib.util.find_spec("edge_tts") is None:
            raise ToolError("edge-tts is not installed.", hint="pip install edge-tts")

        def synth(text: str, out: Path) -> None:
            edge_tts(text, voice_id, out)
    else:
        raise ToolError(f"Unknown provider '{provider}'.", hint="Use 'fish' or 'edge'.")

    folder = proj.path("voice")
    folder.mkdir(parents=True, exist_ok=True)
    entries, jobs = [], []
    for n, beat in enumerate(beats, 1):
        key = _key(provider, voice_id, model, beat["text"])
        entry = {"beat": n, "key": key, "mp3": f"beat_{n:03d}_{key}.mp3", "wav": f"beat_{n:03d}_{key}.wav",
                 "words": word_count(beat["text"])}
        entries.append(entry)
        if not (folder / entry["wav"]).exists():
            jobs.append((entry, beat["text"]))
    log(f"voice ({provider}, {voice_id}): {len(jobs)} beats to generate, {len(entries) - len(jobs)} cached")

    def work(entry: dict, text: str) -> None:
        mp3 = folder / entry["mp3"]
        part = mp3.with_name(mp3.stem + ".part.mp3")
        synth(text, part)
        part.replace(mp3)
        to_trimmed_wav(mp3, folder / entry["wav"])

    budget = Budget()
    failures, finished = [], True
    workers = max(1, min(int(args.get("workers", 3)), 8))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(work, e, t): e for e, t in jobs}
        for done, future in enumerate(as_completed(futures), 1):
            entry = futures[future]
            try:
                future.result()
            except ToolError as exc:
                failures.append({"beat": entry["beat"], "error": str(exc), "hint": exc.hint})
            if done % 10 == 0:
                log(f"voice {done}/{len(jobs)}")
            if budget.expired():
                pool.shutdown(wait=True, cancel_futures=True)
                finished = False
                break

    for entry in entries:
        wav = folder / entry["wav"]
        entry["duration"] = round(wav_seconds(wav), 3) if wav.exists() else None
    write_json(proj.path("voice.json"), {"provider": provider, "voice_id": voice_id, "model": model, "beats": entries})
    keep = {e["mp3"] for e in entries} | {e["wav"] for e in entries}
    for f in folder.iterdir():
        if f.name not in keep and f.suffix in (".mp3", ".wav"):
            f.unlink()

    ready = [e for e in entries if e["duration"]]
    if failures and not ready:
        first = failures[0]
        raise ToolError(f"Voice generation failed: {first['error']}", hint=first["hint"])
    spoken = sum(e["duration"] for e in ready)
    words = sum(e["words"] for e in ready)
    result = {
        "provider": provider,
        "voice_id": voice_id,
        "beats_ready": f"{len(ready)}/{len(entries)}",
        "generated": sum(1 for e, _ in jobs if e["duration"]),
        "narration_minutes": round(spoken / 60, 2),
        "video_minutes_estimate": round((spoken + len(ready) * PAUSE_SEC) / 60, 2),
        "target_minutes": target_minutes(proj, script),
        "spoken_words_per_minute": round(words / (spoken / 60)) if spoken else None,
        "failed": failures,
    }
    if not finished or len(ready) < len(entries):
        result["done"] = False
        result["next"] = "Run generate_voice again to finish the missing beats (finished beats are cached)."
    else:
        result["next"] = "render_video"
    return result
