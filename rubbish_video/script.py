"""Upstream marketing-account script and args.txt parsing.

The eight lines and their timestamps are the v1.1 template from
wmathor/Rubbish-Video-Generator. At a 50 second duration the slots match
that template. Other durations scale the same slots.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from pathlib import Path

# (span_seconds, start_seconds) on the original 50 second timeline.
BASE_DURATION = 50.0
BASE_SLOTS: tuple[tuple[int, int], ...] = (
    (4, 0),
    (7, 4),
    (3, 11),
    (8, 15),
    (6, 23),
    (5, 29),
    (6, 34),
    (5, 40),
)

PHRASE_MAX = 80


@dataclass(frozen=True)
class Line:
    text: str
    span: float
    start: float


def clean_phrase(value: str, label: str) -> str:
    text = value.strip()
    if not text:
        raise ValueError(f"{label} is empty")
    if len(text) > PHRASE_MAX:
        raise ValueError(f"{label} is longer than {PHRASE_MAX} characters")
    if any(unicodedata.category(ch) == "Cc" for ch in text):
        raise ValueError(f"{label} contains control characters")
    return text


def build_lines(body: str, thing: str, other: str, duration: float = BASE_DURATION) -> list[Line]:
    """Fill the upstream template. `duration` scales the original 50s slots."""
    body = clean_phrase(body, "主体 / subject")
    thing = clean_phrase(thing, "事件 / event")
    other = clean_phrase(other, "另一种说法 / other wording")
    if duration <= 0:
        raise ValueError("duration must be positive")
    texts = (
        f"{body}{thing}是怎么回事呢？",
        f"{body}相信大家都很熟悉，但是{body}{thing}是怎么回事呢？",
        "下面就让小编带大家一起了解吧",
        f"{body}{thing}，其实就是{other}",
        f"大家可能会很惊讶{body}怎么会{thing}呢？",
        "但事实就是这样，小编也感到非常惊讶",
        f"这就是关于{body}{thing}的事情了，大家有什么想法呢？",
        "欢迎在评论区告诉小编一起讨论哦！",
    )
    scale = duration / BASE_DURATION
    # At the original length, keep the integer slots exactly.
    if abs(duration - BASE_DURATION) < 1e-6:
        scale = 1.0
    return [
        Line(text=text, span=span * scale, start=start * scale)
        for text, (span, start) in zip(texts, BASE_SLOTS)
    ]


def script_text(lines: list[Line]) -> str:
    rows = [f"[{line.start:.2f}-{line.start + line.span:.2f}] {line.text}" for line in lines]
    return "\n".join(rows) + "\n"


@dataclass(frozen=True)
class ArgsFile:
    body: str
    thing: str
    other: str
    color: str
    fontsize: int
    ignored_legacy_credentials: bool


def load_args_file(path: Path) -> ArgsFile:
    """Read args.txt.

    Line 4 of the upstream file was `APPID;APIKey;APISecret` for iFlytek.
    It is ignored and never logged. Voice is local.
    """
    raw = path.read_text(encoding="utf-8-sig")
    rows = raw.splitlines()
    while len(rows) < 6:
        rows.append("")
    legacy = rows[3].strip()
    ignored = bool(legacy) and legacy.lower() not in {"local", "none", "-"}
    color = rows[4].strip() or "black"
    try:
        fontsize = int(rows[5].strip() or "35")
    except ValueError as exc:
        raise ValueError("args.txt line 6 (font size) must be an integer") from exc
    return ArgsFile(
        body=rows[0].strip(),
        thing=rows[1].strip(),
        other=rows[2].strip(),
        color=color,
        fontsize=fontsize,
        ignored_legacy_credentials=ignored,
    )
