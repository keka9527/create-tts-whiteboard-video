#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import wave
from pathlib import Path

from workflow_common import parse_cues


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as reader:
        return reader.getnframes() / reader.getframerate()


def cache_key(text: str, language: str, speed: float, sample_rate: int, speaker: str) -> str:
    value = json.dumps(
        {
            "text": text,
            "language": language,
            "speed": speed,
            "sampleRate": sample_rate,
            "speaker": speaker,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_wav(source: Path, output: Path, sample_rate: int) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg is required to normalize MeloTTS output")
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-ar",
            str(sample_rate),
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(output),
        ],
        check=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate one reusable MeloTTS WAV segment per narration cue."
    )
    parser.add_argument("--cues", type=Path, required=True, help="SRT or UTF-8 text; one cue per non-empty line")
    parser.add_argument("--segments-dir", type=Path, required=True)
    parser.add_argument("--language", default="ZH")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--speed", type=float, default=1.08)
    parser.add_argument("--sample-rate", type=int, default=44100)
    parser.add_argument("--speaker-id", default=None, help="MeloTTS speaker id; defaults to the first Chinese speaker")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    cues = parse_cues(args.cues)
    args.segments_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = args.segments_dir / "tts-segments.json"
    previous = {}
    if manifest_path.exists():
        try:
            previous_rows = json.loads(manifest_path.read_text(encoding="utf-8"))
            previous = {int(row["index"]): row for row in previous_rows}
        except (OSError, ValueError, KeyError, TypeError):
            previous = {}

    speaker_label = args.speaker_id or "first"
    pending = []
    reports = []
    for cue in cues:
        output = args.segments_dir / f"segment-{cue.index:02d}-raw.wav"
        key = cache_key(cue.text, args.language, args.speed, args.sample_rate, speaker_label)
        cached = previous.get(cue.index, {})
        valid = False
        if not args.force and output.exists() and cached.get("cacheKey") == key:
            try:
                valid = wav_duration(output) > 0
            except (wave.Error, OSError, ZeroDivisionError):
                valid = False
        row = {
            "index": cue.index,
            "text": cue.text,
            "cacheKey": key,
            "path": output.name,
        }
        if valid:
            row["durationSec"] = round(wav_duration(output), 3)
            reports.append(row)
            print(f"CACHE={output}", flush=True)
        else:
            pending.append((cue, output, row))

    if pending:
        try:
            from melo.api import TTS
        except ImportError:
            print(
                "[err] MeloTTS is not installed in this Python environment. "
                "Run this script with the project's MeloTTS virtual-environment Python.",
                file=sys.stderr,
            )
            return 2

        print(f"Loading MeloTTS language={args.language} device={args.device}...", flush=True)
        model = TTS(language=args.language, device=args.device)
        if args.speaker_id is None:
            speaker_id = next(iter(model.hps.data.spk2id.values()))
        else:
            try:
                speaker_id = int(args.speaker_id)
            except ValueError:
                speaker_id = model.hps.data.spk2id[args.speaker_id]

        for position, (cue, output, row) in enumerate(pending, start=1):
            model_path = output.with_name(output.stem + "-model.wav")
            print(f"TTS={position}/{len(pending)} cue={cue.index}: {cue.text}", flush=True)
            model.tts_to_file(
                cue.text,
                speaker_id,
                output_path=str(model_path),
                speed=args.speed,
                quiet=True,
            )
            normalize_wav(model_path, output, args.sample_rate)
            model_path.unlink(missing_ok=True)
            row["durationSec"] = round(wav_duration(output), 3)
            reports.append(row)

    reports.sort(key=lambda row: row["index"])
    manifest_path.write_text(
        json.dumps(reports, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"SEGMENTS_DIR={args.segments_dir.resolve()}")
    print(f"MANIFEST={manifest_path.resolve()}")
    print(f"CUE_COUNT={len(cues)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
