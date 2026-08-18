#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import wave
from collections import defaultdict
from pathlib import Path

from workflow_common import natural_key, normalize_text, parse_cues, timestamp_srt


def read_pcm16_mono(path: Path, expected_rate: int | None) -> tuple[bytes, int, int]:
    with wave.open(str(path), "rb") as reader:
        channels = reader.getnchannels()
        sample_width = reader.getsampwidth()
        sample_rate = reader.getframerate()
        frame_count = reader.getnframes()
        payload = reader.readframes(frame_count)
    if channels != 1 or sample_width != 2:
        raise ValueError(f"Expected mono PCM16 WAV, got channels={channels}, width={sample_width}: {path}")
    if expected_rate is not None and sample_rate != expected_rate:
        raise ValueError(f"Unexpected sample rate {sample_rate}; expected {expected_rate}: {path}")
    return payload, sample_rate, frame_count


def write_pcm16_mono(path: Path, payload: bytes, sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(payload)


def silence(milliseconds: int, sample_rate: int) -> tuple[bytes, int]:
    samples = round(milliseconds * sample_rate / 1000)
    return b"\x00\x00" * samples, samples


def allocate_durations(total_ms: int, weights: list[int], minimum_ms: int = 450) -> list[int]:
    if len(weights) == 1:
        return [total_ms]
    floor_total = minimum_ms * len(weights)
    if floor_total >= total_ms:
        base = total_ms // len(weights)
        result = [base] * len(weights)
        result[-1] += total_ms - sum(result)
        return result
    remainder = total_ms - floor_total
    weight_sum = max(1, sum(weights))
    result = [minimum_ms + round(remainder * weight / weight_sum) for weight in weights]
    result[-1] += total_ms - sum(result)
    return result


def find_scene_image(annotation: Path) -> Path:
    base = annotation.name[: -len(".annotation.json")]
    for suffix in (".png", ".jpg", ".jpeg"):
        candidate = annotation.with_name(base + suffix)
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"No image found for {annotation.name}")


def scene_cue_count(data: dict, annotation: Path) -> int:
    seen: set[str] = set()
    ordered: list[str] = []
    for element in sorted(data.get("elements", []), key=lambda item: item["sequence"]):
        subtitle = element.get("subtitle", "")
        key = normalize_text(subtitle)
        if not key:
            raise ValueError(f"Element {element.get('id')} has no subtitle: {annotation}")
        if key not in seen:
            seen.add(key)
            ordered.append(key)
    if not ordered:
        raise ValueError(f"No cue-linked elements found in {annotation}")
    return len(ordered)


def normalize_exact_duration(source: Path, output: Path, sample_rate: int, target_samples: int) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg is required for audio normalization")
    normalized = output.with_name(output.stem + "-normalized.wav")
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-af",
            "highpass=f=65,lowpass=f=14500,loudnorm=I=-16:TP=-1.5:LRA=7",
            "-ar",
            str(sample_rate),
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(normalized),
        ],
        check=True,
    )
    payload, _, frames = read_pcm16_mono(normalized, sample_rate)
    if frames < target_samples:
        payload += b"\x00\x00" * (target_samples - frames)
    elif frames > target_samples:
        payload = payload[: target_samples * 2]
    write_pcm16_mono(output, payload, sample_rate)
    normalized.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Make measured TTS audio the master clock for subtitles and whiteboard annotations."
    )
    parser.add_argument("--cues", type=Path, required=True, help="SRT or one-cue-per-line UTF-8 text")
    parser.add_argument("--source-assets", type=Path, required=True)
    parser.add_argument("--segments-dir", type=Path, required=True)
    parser.add_argument("--output-assets", type=Path, required=True)
    parser.add_argument("--output-audio", type=Path, required=True)
    parser.add_argument("--output-srt", type=Path, required=True)
    parser.add_argument("--lead-ms", type=int, default=150)
    parser.add_argument("--inter-cue-ms", type=int, default=300)
    parser.add_argument("--scene-tail-ms", type=int, default=350)
    parser.add_argument("--final-tail-ms", type=int, default=500)
    parser.add_argument("--draw-lead-ms", type=int, default=50)
    parser.add_argument("--draw-finish-early-ms", type=int, default=180)
    parser.add_argument("--inter-element-ms", type=int, default=60)
    parser.add_argument("--max-draw-still-ms", type=int, default=800)
    parser.add_argument("--max-voice-gap-ms", type=int, default=550)
    args = parser.parse_args()

    cues = parse_cues(args.cues)
    annotations = sorted(args.source_assets.glob("*.annotation.json"), key=natural_key)
    if not annotations:
        raise ValueError(f"No *.annotation.json files found in {args.source_assets}")

    scene_data = []
    cue_cursor = 0
    for annotation in annotations:
        data = json.loads(annotation.read_text(encoding="utf-8"))
        count = scene_cue_count(data, annotation)
        selected = cues[cue_cursor : cue_cursor + count]
        if len(selected) != count:
            raise ValueError(f"Not enough cues for {annotation.name}; needs {count}")
        annotation_keys = []
        seen = set()
        for element in sorted(data["elements"], key=lambda item: item["sequence"]):
            key = normalize_text(element["subtitle"])
            if key not in seen:
                seen.add(key)
                annotation_keys.append(key)
        cue_keys = [normalize_text(cue.text) for cue in selected]
        if annotation_keys != cue_keys:
            raise ValueError(
                f"Cue text mismatch in {annotation.name}. "
                "Each distinct element.subtitle must match the next narration cue exactly."
            )
        scene_data.append((annotation, data, selected))
        cue_cursor += count
    if cue_cursor != len(cues):
        raise ValueError(f"{len(cues) - cue_cursor} narration cues are not assigned to any scene")

    segment_audio: dict[int, tuple[bytes, int]] = {}
    sample_rate: int | None = None
    for cue in cues:
        path = args.segments_dir / f"segment-{cue.index:02d}-raw.wav"
        if not path.exists():
            raise FileNotFoundError(f"Missing TTS segment: {path}")
        payload, detected_rate, frames = read_pcm16_mono(path, sample_rate)
        sample_rate = detected_rate
        segment_audio[cue.index] = (payload, frames)
    if sample_rate is None:
        raise ValueError("No audio segments loaded")

    args.output_assets.mkdir(parents=True, exist_ok=True)
    args.output_audio.parent.mkdir(parents=True, exist_ok=True)
    args.output_srt.parent.mkdir(parents=True, exist_ok=True)

    track_parts: list[bytes] = []
    subtitle_rows: list[dict] = []
    scene_reports: list[dict] = []
    all_cue_rows: list[dict] = []
    global_samples = 0

    for scene_index, (annotation_path, data, selected_cues) in enumerate(scene_data):
        elements_by_subtitle: dict[str, list[dict]] = defaultdict(list)
        for element in sorted(data["elements"], key=lambda item: item["sequence"]):
            elements_by_subtitle[normalize_text(element["subtitle"])].append(element)

        lead_payload, lead_samples = silence(args.lead_ms, sample_rate)
        scene_parts = [lead_payload]
        local_samples = lead_samples
        scene_global_start_samples = global_samples
        scene_rows = []

        for cue_position, cue in enumerate(selected_cues):
            elements = elements_by_subtitle[normalize_text(cue.text)]
            audio_payload, audio_samples = segment_audio[cue.index]
            voice_start_local_ms = round(local_samples * 1000 / sample_rate)
            voice_end_local_samples = local_samples + audio_samples
            voice_end_local_ms = round(voice_end_local_samples * 1000 / sample_rate)
            voice_start_global_ms = round((scene_global_start_samples + local_samples) * 1000 / sample_rate)
            voice_end_global_ms = round((scene_global_start_samples + voice_end_local_samples) * 1000 / sample_rate)

            draw_start_ms = max(0, voice_start_local_ms - args.draw_lead_ms)
            draw_end_ms = voice_end_local_ms - args.draw_finish_early_ms
            gap_total = args.inter_element_ms * (len(elements) - 1)
            available_ms = draw_end_ms - draw_start_ms - gap_total
            if available_ms <= 0:
                raise ValueError(f"Cue {cue.index} has no usable drawing window")
            weights = [max(1, int(item["reveal"]["durationMs"])) for item in elements]
            allocated = allocate_durations(available_ms, weights)

            cursor_ms = draw_start_ms
            for element, duration_ms in zip(elements, allocated):
                original_start = int(element["reveal"]["startMs"])
                original_duration = int(element["reveal"]["durationMs"])
                element["reveal"]["startMs"] = cursor_ms
                element["reveal"]["durationMs"] = duration_ms
                element["audioTiming"] = {
                    "cueIndex": cue.index,
                    "voiceStartMs": voice_start_local_ms,
                    "voiceEndMs": voice_end_local_ms,
                    "targetDrawEndMs": draw_end_ms,
                    "originalStartMs": original_start,
                    "originalDurationMs": original_duration,
                }
                cursor_ms += duration_ms + args.inter_element_ms
            actual_draw_end_ms = cursor_ms - args.inter_element_ms
            global_scene_start_ms = round(scene_global_start_samples * 1000 / sample_rate)
            cue_row = {
                "cueIndex": cue.index,
                "subtitle": cue.text,
                "sceneIndex": scene_index + 1,
                "voiceStartMs": voice_start_local_ms,
                "voiceEndMs": voice_end_local_ms,
                "globalVoiceStartMs": voice_start_global_ms,
                "globalVoiceEndMs": voice_end_global_ms,
                "drawStartMs": draw_start_ms,
                "drawEndMs": actual_draw_end_ms,
                "globalDrawStartMs": global_scene_start_ms + draw_start_ms,
                "globalDrawEndMs": global_scene_start_ms + actual_draw_end_ms,
                "elementIds": [item["id"] for item in elements],
            }
            scene_rows.append(cue_row)
            all_cue_rows.append(cue_row)
            subtitle_rows.append(
                {
                    "index": cue.index,
                    "startMs": voice_start_global_ms,
                    "endMs": voice_end_global_ms,
                    "text": cue.text,
                }
            )

            scene_parts.append(audio_payload)
            local_samples = voice_end_local_samples
            if cue_position < len(selected_cues) - 1:
                gap_payload, gap_samples = silence(args.inter_cue_ms, sample_rate)
                scene_parts.append(gap_payload)
                local_samples += gap_samples

        tail_ms = args.final_tail_ms if scene_index == len(scene_data) - 1 else args.scene_tail_ms
        tail_payload, tail_samples = silence(tail_ms, sample_rate)
        scene_parts.append(tail_payload)
        local_samples += tail_samples
        scene_payload = b"".join(scene_parts)
        scene_duration_ms = round(local_samples * 1000 / sample_rate)

        data["sceneDurationMs"] = scene_duration_ms
        data["timingMode"] = "tts-master-natural"
        data["timingBasis"] = {
            "leadMs": args.lead_ms,
            "interCueMs": args.inter_cue_ms,
            "sceneTailMs": tail_ms,
            "drawLeadMs": args.draw_lead_ms,
            "drawFinishesBeforeVoiceEndMs": args.draw_finish_early_ms,
            "interElementGapMs": args.inter_element_ms,
        }
        output_annotation = args.output_assets / annotation_path.name
        output_annotation.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        source_image = find_scene_image(annotation_path)
        shutil.copy2(source_image, args.output_assets / source_image.name)

        track_parts.append(scene_payload)
        scene_reports.append(
            {
                "sceneId": data.get("sceneId", annotation_path.stem),
                "sceneDurationMs": scene_duration_ms,
                "globalStartMs": round(scene_global_start_samples * 1000 / sample_rate),
                "cues": scene_rows,
            }
        )
        global_samples += local_samples

    combined = b"".join(track_parts)
    pre_loudnorm = args.output_audio.with_name(args.output_audio.stem + "-pre-loudnorm.wav")
    write_pcm16_mono(pre_loudnorm, combined, sample_rate)
    normalize_exact_duration(pre_loudnorm, args.output_audio, sample_rate, global_samples)
    pre_loudnorm.unlink(missing_ok=True)

    blocks = [
        f"{row['index']}\n{timestamp_srt(row['startMs'])} --> {timestamp_srt(row['endMs'])}\n{row['text']}"
        for row in subtitle_rows
    ]
    args.output_srt.write_text("\n\n".join(blocks) + "\n", encoding="utf-8-sig")

    total_duration_ms = round(global_samples * 1000 / sample_rate)
    draw_stills = []
    voice_gaps = []
    for index, row in enumerate(all_cue_rows):
        if index + 1 < len(all_cue_rows):
            next_row = all_cue_rows[index + 1]
            draw_stills.append(next_row["globalDrawStartMs"] - row["globalDrawEndMs"])
            voice_gaps.append(next_row["globalVoiceStartMs"] - row["globalVoiceEndMs"])
        else:
            draw_stills.append(total_duration_ms - row["globalDrawEndMs"])
    max_draw_still_ms = max(draw_stills, default=0)
    max_voice_gap_ms = max(voice_gaps, default=0)
    if max_draw_still_ms > args.max_draw_still_ms:
        raise RuntimeError(f"Drawing stall {max_draw_still_ms}ms exceeds {args.max_draw_still_ms}ms")
    if max_voice_gap_ms > args.max_voice_gap_ms:
        raise RuntimeError(f"Voice gap {max_voice_gap_ms}ms exceeds {args.max_voice_gap_ms}ms")

    report = {
        "timingMode": "tts-master-natural",
        "sampleRate": sample_rate,
        "cueCount": len(cues),
        "totalDurationMs": total_duration_ms,
        "maxDrawStillMs": max_draw_still_ms,
        "maxVoiceGapMs": max_voice_gap_ms,
        "thresholds": {
            "maxDrawStillMs": args.max_draw_still_ms,
            "maxVoiceGapMs": args.max_voice_gap_ms,
        },
        "scenes": scene_reports,
    }
    report_path = args.output_assets / "natural-audio-timeline-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"OUTPUT_AUDIO={args.output_audio.resolve()}")
    print(f"OUTPUT_SRT={args.output_srt.resolve()}")
    print(f"OUTPUT_ASSETS={args.output_assets.resolve()}")
    print(f"TIMING_REPORT={report_path.resolve()}")
    print(f"TOTAL_DURATION_MS={total_duration_ms}")
    print(f"MAX_DRAW_STILL_MS={max_draw_still_ms}")
    print(f"MAX_VOICE_GAP_MS={max_voice_gap_ms}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
