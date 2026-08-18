from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


SRT_BLOCK_RE = re.compile(
    r"(?ms)^\s*(\d+)\s*\n"
    r"(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*\n"
    r"(.*?)(?=\n\s*\n|\Z)"
)


@dataclass(frozen=True)
class Cue:
    index: int
    text: str


def normalize_text(value: str) -> str:
    replacements = {
        "\u201c": '"',
        "\u201d": '"',
        "\u2018": "'",
        "\u2019": "'",
        "\u3000": "",
    }
    normalized = re.sub(r"\s+", "", value)
    for source, target in replacements.items():
        normalized = normalized.replace(source, target)
    return normalized


def parse_cues(path: Path) -> list[Cue]:
    content = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    if path.suffix.lower() == ".srt" or "-->" in content:
        cues = []
        for match in SRT_BLOCK_RE.finditer(content):
            text = " ".join(
                line.strip() for line in match.group(4).splitlines() if line.strip()
            )
            cues.append(Cue(index=int(match.group(1)), text=text))
    else:
        rows = [line.strip() for line in content.splitlines() if line.strip()]
        cues = [Cue(index=index, text=text) for index, text in enumerate(rows, start=1)]
    if not cues:
        raise ValueError(f"No narration cues found in {path}")
    expected = list(range(1, len(cues) + 1))
    actual = [cue.index for cue in cues]
    if actual != expected:
        raise ValueError(f"Cue indexes must be continuous from 1; got {actual}")
    return cues


def timestamp_srt(milliseconds: int) -> str:
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def natural_key(path: Path) -> list[object]:
    return [int(token) if token.isdigit() else token.lower() for token in re.split(r"(\d+)", path.name)]
