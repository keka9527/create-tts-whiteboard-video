#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from workflow_common import natural_key


SCRIPT_DIR = Path(__file__).resolve().parent


def require_executable(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise RuntimeError(f"Required executable not found on PATH: {name}")
    return path


def renderer_python(requested: Path | None) -> Path:
    if requested is not None:
        if not requested.exists():
            raise FileNotFoundError(f"Renderer Python not found: {requested}")
        return requested
    bundled = SCRIPT_DIR.parent / ".venv" / ("Scripts/python.exe" if sys.platform.startswith("win") else "bin/python")
    if bundled.exists():
        return bundled
    result = subprocess.run(
        [sys.executable, str(SCRIPT_DIR / "prepare_env.py")],
        check=True,
        capture_output=True,
        text=True,
    )
    for line in reversed(result.stdout.splitlines()):
        if line.startswith("ENV_PY="):
            return Path(line.split("=", 1)[1].strip())
    raise RuntimeError("prepare_env.py did not report ENV_PY")


def scene_image(annotation: Path) -> Path:
    base = annotation.name[: -len(".annotation.json")]
    for suffix in (".png", ".jpg", ".jpeg"):
        candidate = annotation.with_name(base + suffix)
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"No image found for {annotation.name}")


def digest_files(paths: list[Path], settings: dict) -> str:
    digest = hashlib.sha256(json.dumps(settings, sort_keys=True).encode("utf-8"))
    for path in paths:
        digest.update(path.name.encode("utf-8"))
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    return digest.hexdigest()


def run_checked(command: list[str], cwd: Path | None = None) -> None:
    print("RUN=" + " ".join(command), flush=True)
    subprocess.run(command, cwd=str(cwd) if cwd else None, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render TTS-timed whiteboard scenes and produce a subtitled H.264/AAC final video."
    )
    parser.add_argument("--timed-assets", type=Path, required=True)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--srt", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--renderer-python", type=Path, default=None)
    parser.add_argument("--subtitle-font", default="Microsoft YaHei")
    parser.add_argument("--subtitle-size", type=int, default=16)
    parser.add_argument(
        "--reveal-order",
        choices=["semantic", "spatial-scan"],
        default="semantic",
        help=(
            "Semantic subject-by-subject drawing is the default; "
            "spatial-scan keeps the optional whole-scene left-to-right sweep."
        ),
    )
    parser.add_argument("--spatial-band-px", type=int, default=180)
    parser.add_argument("--spatial-color-lag-ms", type=int, default=320)
    parser.add_argument("--hide-hand", action="store_true", help="关闭默认的手持画笔动画")
    parser.add_argument(
        "--hand",
        type=Path,
        default=SCRIPT_DIR.parent / "assets" / "drawing-hand.png",
        help="带透明通道的手部 PNG；默认使用 Skill 内置通用素材",
    )
    parser.add_argument("--hand-height", type=int, default=260)
    parser.add_argument("--tip-smoothing", type=float, default=0.30)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    ffmpeg = require_executable("ffmpeg")
    require_executable("ffprobe")
    py = renderer_python(args.renderer_python)
    renderer = SCRIPT_DIR / "render_stream_whiteboard.py"
    merger = SCRIPT_DIR / "merge_scenes.py"
    annotations = sorted(args.timed_assets.glob("*.annotation.json"), key=natural_key)
    if not annotations:
        raise ValueError(f"No timed annotations found in {args.timed_assets}")
    if not args.audio.exists() or not args.srt.exists():
        raise FileNotFoundError("Audio or SRT input is missing")
    if not args.hide_hand and not args.hand.exists():
        raise FileNotFoundError(f"Hand PNG is missing: {args.hand}")

    scene_dir = args.work_dir / "rendered-scenes-tts-master"
    scene_dir.mkdir(parents=True, exist_ok=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    manifest_path = scene_dir / "render-manifest.json"
    previous = {}
    if manifest_path.exists():
        try:
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous = {}

    settings = {
        "maskPolicy": "exclusive-owner-v2",
        "revealOrder": args.reveal_order,
        "spatialBandPx": args.spatial_band_px,
        "spatialColorLagMs": args.spatial_color_lag_ms,
        "fps": 60,
        "gridEdge": 5,
        "motionProfile": "smooth",
        "inkPath": "grid",
        "colorFill": "contour-wipe",
        "pause": "off",
        "handVisible": not args.hide_hand,
        "handHeight": max(32, args.hand_height),
        "tipSmoothing": min(1.0, max(0.01, args.tip_smoothing)),
    }
    scene_outputs = []
    manifest = {}
    for annotation in annotations:
        image = scene_image(annotation)
        base = annotation.name[: -len(".annotation.json")]
        output = scene_dir / f"{base}-whiteboard.mp4"
        mask_report = scene_dir / f"{base}-mask-report.json"
        mask_preview = scene_dir / f"{base}-legacy-dead-zone-preview.png"
        signature_paths = [annotation, image, renderer, SCRIPT_DIR / "stream_render.py"]
        if not args.hide_hand:
            signature_paths.append(args.hand)
        signature = digest_files(signature_paths, settings)
        mask_report_ok = False
        if mask_report.exists() and mask_preview.exists():
            try:
                report_data = json.loads(mask_report.read_text(encoding="utf-8"))
                mask_report_ok = (
                    report_data.get("policy") == settings["maskPolicy"]
                    and report_data.get("coverageGapPixels") == 0
                )
            except (OSError, ValueError):
                mask_report_ok = False
        cached = (
            not args.force
            and output.exists()
            and output.stat().st_size > 1024
            and mask_report_ok
            and previous.get(base, {}).get("signature") == signature
        )
        if cached:
            print(f"CACHE_SCENE={output.resolve()}")
        else:
            command = [str(py), str(renderer), str(image), str(annotation), str(output)]
            if args.hide_hand:
                command.append("--bare-tip")
            else:
                command.append(str(args.hand))
            command.extend(
                [
                    "--ink-path",
                    "grid",
                    "--color-fill",
                    "contour-wipe",
                    "--pause",
                    "off",
                    "--fps",
                    "60",
                    "--grid-edge",
                    "5",
                    "--motion-profile",
                    "smooth",
                    "--reveal-order",
                    args.reveal_order,
                    "--spatial-band-px",
                    str(args.spatial_band_px),
                    "--spatial-color-lag-ms",
                    str(args.spatial_color_lag_ms),
                    "--hand-height",
                    str(max(32, args.hand_height)),
                    "--tip-smoothing",
                    str(min(1.0, max(0.01, args.tip_smoothing))),
                    "--mask-report",
                    str(mask_report),
                    "--mask-preview",
                    str(mask_preview),
                ]
            )
            run_checked(command)
        manifest[base] = {"signature": signature, "output": output.name}
        scene_outputs.append(output)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    merged = args.work_dir / "whiteboard-master-60fps.mp4"
    run_checked(
        [str(py), str(merger), "--inputs", *[str(path) for path in scene_outputs], "--output", str(merged)]
    )

    subtitle_style = (
        f"FontName={args.subtitle_font},FontSize={args.subtitle_size},"
        "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
        "BorderStyle=1,Outline=2,Shadow=0,Alignment=2,MarginV=48"
    )
    with tempfile.TemporaryDirectory(prefix="wb-burn-", dir=str(args.output.parent)) as temp_name:
        temp_dir = Path(temp_name)
        local_srt = temp_dir / "captions.srt"
        local_srt.write_text(args.srt.read_text(encoding="utf-8-sig"), encoding="utf-8-sig")
        video_filter = (
            "scale=1920:1080:force_original_aspect_ratio=decrease,"
            "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,"
            f"subtitles=captions.srt:force_style='{subtitle_style}',fps=30"
        )
        run_checked(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(merged.resolve()),
                "-i",
                str(args.audio.resolve()),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-vf",
                video_filter,
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "18",
                "-pix_fmt",
                "yuv420p",
                "-fps_mode",
                "cfr",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-ar",
                "44100",
                "-ac",
                "1",
                "-shortest",
                "-movflags",
                "+faststart",
                str(args.output.resolve()),
            ],
            cwd=temp_dir,
        )

    print(f"OUTPUT_VIDEO={args.output.resolve()}")
    print(f"SCENE_DIR={scene_dir.resolve()}")
    print(f"MERGED_VIDEO={merged.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
