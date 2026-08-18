#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

from workflow_common import parse_cues


FREEZE_DURATION_RE = re.compile(r"freeze_duration:\s*([0-9.]+)")


def require(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise RuntimeError(f"Required executable not found: {name}")
    return path


def probe(ffprobe: str, path: Path) -> dict:
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return json.loads(result.stdout)


def duration_seconds(data: dict) -> float:
    return float(data["format"]["duration"])


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the final TTS-driven whiteboard video and timing report.")
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--srt", type=Path, required=True)
    parser.add_argument("--timing-report", type=Path, required=True)
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--max-duration-drift-sec", type=float, default=0.12)
    parser.add_argument("--max-freeze-sec", type=float, default=0.80)
    parser.add_argument("--skip-freeze-scan", action="store_true")
    parser.add_argument(
        "--fail-on-freeze",
        action="store_true",
        help="Treat generic freezedetect candidates as failures; normally they are advisory for local drawing motion",
    )
    args = parser.parse_args()

    ffprobe = require("ffprobe")
    ffmpeg = require("ffmpeg")
    video_data = probe(ffprobe, args.video)
    audio_data = probe(ffprobe, args.audio)
    video_stream = next((stream for stream in video_data["streams"] if stream["codec_type"] == "video"), None)
    audio_stream = next((stream for stream in video_data["streams"] if stream["codec_type"] == "audio"), None)
    if video_stream is None or audio_stream is None:
        raise RuntimeError("Final file must contain both video and audio streams")

    fps = float(Fraction(video_stream["avg_frame_rate"]))
    video_duration = duration_seconds(video_data)
    audio_duration = duration_seconds(audio_data)
    drift = abs(video_duration - audio_duration)
    cues = parse_cues(args.srt)
    timing = json.loads(args.timing_report.read_text(encoding="utf-8"))

    errors = []
    if (video_stream.get("width"), video_stream.get("height")) != (1920, 1080):
        errors.append(f"expected 1920x1080, got {video_stream.get('width')}x{video_stream.get('height')}")
    if video_stream.get("codec_name") != "h264":
        errors.append(f"expected H.264, got {video_stream.get('codec_name')}")
    if audio_stream.get("codec_name") != "aac":
        errors.append(f"expected AAC, got {audio_stream.get('codec_name')}")
    if abs(fps - 30.0) > 0.01:
        errors.append(f"expected 30fps CFR, got {fps:.4f}")
    if drift > args.max_duration_drift_sec:
        errors.append(f"video/audio duration drift {drift:.3f}s exceeds {args.max_duration_drift_sec:.3f}s")
    if timing.get("cueCount") != len(cues):
        errors.append(f"timing cue count {timing.get('cueCount')} != SRT cue count {len(cues)}")
    if timing.get("maxDrawStillMs", 10**9) > 800:
        errors.append(f"planned drawing stall is {timing.get('maxDrawStillMs')}ms")
    if timing.get("maxVoiceGapMs", 10**9) > 550:
        errors.append(f"planned voice gap is {timing.get('maxVoiceGapMs')}ms")

    freeze_durations = []
    filter_chain = None if args.skip_freeze_scan else f"freezedetect=n=-60dB:d={args.max_freeze_sec}"
    decode_command = [ffmpeg, "-hide_banner", "-v", "info" if filter_chain else "error", "-i", str(args.video)]
    if filter_chain:
        decode_command.extend(["-vf", filter_chain])
    decode_command.extend(["-f", "null", "-"])
    decoded = subprocess.run(
        decode_command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if decoded.returncode != 0:
        errors.append("full video decode failed")
    if filter_chain:
        freeze_durations = [float(value) for value in FREEZE_DURATION_RE.findall(decoded.stderr)]
        too_long = [value for value in freeze_durations if value > args.max_freeze_sec + 0.01]
        if too_long and args.fail_on_freeze:
            errors.append(f"detected visual freezes longer than {args.max_freeze_sec:.2f}s: {too_long}")

    report = {
        "status": "PASS" if not errors else "FAIL",
        "video": str(args.video.resolve()),
        "durationSec": round(video_duration, 3),
        "audioDurationSec": round(audio_duration, 3),
        "durationDriftSec": round(drift, 4),
        "resolution": [video_stream.get("width"), video_stream.get("height")],
        "fps": round(fps, 4),
        "videoCodec": video_stream.get("codec_name"),
        "audioCodec": audio_stream.get("codec_name"),
        "cueCount": len(cues),
        "maxDrawStillMs": timing.get("maxDrawStillMs"),
        "maxVoiceGapMs": timing.get("maxVoiceGapMs"),
        "freezeDurationsSec": freeze_durations,
        "freezeScanAdvisory": not args.fail_on_freeze,
        "errors": errors,
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
