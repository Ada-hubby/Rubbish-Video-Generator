#!/usr/bin/env python3
"""Local entry point for the Rubbish-Video-Generator assembler.

The upstream script called iFlytek's streaming TTS. This entry point does
not. Speech is espeak-ng or a local Piper ONNX voice.

Examples
--------
python3 run.py --check
python3 run.py --download-voice
python3 run.py --serve
python3 run.py --synthetic --duration 50 --output final.mp4
python3 run.py --video in.mp4 --bgm in.mp3 --args args.txt --output final.mp4
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rubbish_video.pipeline import PipelineError, build_video, doctor
from rubbish_video.script import load_args_file
from rubbish_video.tts import TTSError, download_voice
from rubbish_video.webui import serve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local marketing-template video assembler (no paid APIs)")
    parser.add_argument("--video", type=Path, help="Footage to loop or trim. Not generated from text.")
    parser.add_argument("--bgm", type=Path, help="Background audio, ducked to -20 dB.")
    parser.add_argument("--narration", type=Path, help="Use this audio instead of TTS. Not line-synced.")
    parser.add_argument("--args", type=Path, help="Upstream args.txt. Line 4 credentials are ignored.")
    parser.add_argument("--body", help="主体. Overrides args.txt line 1.")
    parser.add_argument("--thing", help="事件. Overrides args.txt line 2.")
    parser.add_argument("--other", help="另一种说法. Overrides args.txt line 3.")
    parser.add_argument("--color", help="Subtitle color name or hex. Default from args.txt or black.")
    parser.add_argument("--fontsize", type=int, help="Subtitle size in pixels. Default from args.txt or 35.")
    parser.add_argument("--duration", type=float, default=50.0, help="Seconds. 50 matches the upstream slots.")
    parser.add_argument("--tts", choices=("auto", "espeak", "piper"), default="auto")
    parser.add_argument("--output", type=Path, default=Path("final.mp4"))
    parser.add_argument("--synthetic", action="store_true", help="Generate a test pattern and a tone. No input files.")
    parser.add_argument("--pattern", choices=("testsrc", "gray"), default="testsrc")
    parser.add_argument("--keep-temp", action="store_true")
    parser.add_argument("--serve", action="store_true", help="Serve the page on 127.0.0.1 only.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--download-voice", action="store_true", help="Download the pinned Piper voice and exit.")
    parser.add_argument("--check", action="store_true", help="Print local dependency status and exit.")
    args = parser.parse_args(argv)

    try:
        if args.download_voice:
            path = download_voice()
            print(f"Saved {path}")
            print("SHA-256 checked. The file stays in models/ and is not uploaded anywhere.")
            return 0
        if args.check:
            for line in doctor():
                print(line)
            return 0
        if args.serve:
            serve(args.port)
            return 0
        return _build(args)
    except (PipelineError, TTSError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _build(args: argparse.Namespace) -> int:
    loaded = None
    args_path = args.args
    if args_path is None and not args.body and Path("args.txt").is_file():
        args_path = Path("args.txt")
    if args_path is not None:
        loaded = load_args_file(args_path)
        if loaded.ignored_legacy_credentials:
            print(
                "Ignoring args.txt line 4. iFlytek credentials are not read out or sent. "
                "Voice is local."
            )
    body = args.body or (loaded.body if loaded else "")
    thing = args.thing or (loaded.thing if loaded else "")
    other = args.other or (loaded.other if loaded else "")
    color = args.color or (loaded.color if loaded else "black")
    fontsize = args.fontsize if args.fontsize is not None else (loaded.fontsize if loaded else 35)

    video = args.video
    bgm = args.bgm
    synthetic_video = False
    synthetic_bgm = False
    if args.synthetic:
        if video or bgm:
            raise PipelineError("Use either --synthetic or --video/--bgm, not both.")
        synthetic_video = True
        synthetic_bgm = True
    else:
        if video is None and Path("in.mp4").is_file():
            video = Path("in.mp4")
        if bgm is None and Path("in.mp3").is_file():
            bgm = Path("in.mp3")
        if video is None and bgm is None:
            raise PipelineError(
                "No in.mp4 / in.mp3 and no --video/--bgm. "
                "Pass input files, or run with --synthetic to use a test pattern."
            )

    result = build_video(
        output=args.output,
        video=video,
        bgm=bgm,
        narration=args.narration,
        body=body,
        thing=thing,
        other=other,
        color=color,
        fontsize=fontsize,
        duration=args.duration,
        tts=args.tts,
        synthetic_video=synthetic_video,
        synthetic_bgm=synthetic_bgm,
        pattern=args.pattern,
        keep_temp=args.keep_temp,
    )
    print(f"output: {result.output.resolve()}")
    print(f"voice: {result.engine}")
    print(f"picture: {result.width}x{result.height}, {result.duration:.0f}s, {result.elapsed_sec:.1f}s render, {result.output_bytes} bytes")
    print(f"script: {result.script_path}")
    for warning in result.warnings:
        print(f"warning: {warning}")
    print("This is a template composite, not generated footage, and not a claim about reach or virality.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
