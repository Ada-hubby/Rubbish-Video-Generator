"""Assemble the template video with system ffmpeg. No shell, no network.

What this produces
------------------
A video of your own footage (or a labeled test pattern), with the eight-line
营销号 script burned in as subtitles and spoken by a local voice, over a
quiet background track.

What this does not produce
--------------------------
Footage, scenes, faces, or photorealistic video from a text prompt.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .script import BASE_DURATION, Line, build_lines, script_text, validate_lines
from .tts import Engine, TTSError, resolve_engine, wav_duration

ROOT = Path(__file__).resolve().parent.parent
MIN_DURATION = 8.0
MAX_DURATION = 180.0
MAX_PIXELS = 1920 * 1080

NAMED_COLORS = {
    "black": "000000",
    "white": "FFFFFF",
    "yellow": "00FFFF",
    "red": "0000FF",
    "blue": "FF0000",
    "green": "008000",
    "cyan": "FFFF00",
    "orange": "0080FF",
}


class PipelineError(RuntimeError):
    pass


@dataclass
class LineReport:
    text: str
    start: float
    span: float
    speech_sec: float
    atempo: float
    trimmed: bool


@dataclass
class BuildResult:
    output: Path
    script_path: Path
    engine: str
    duration: float
    width: int
    height: int
    elapsed_sec: float
    output_bytes: int
    warnings: list[str] = field(default_factory=list)
    lines: list[LineReport] = field(default_factory=list)
    synthetic_video: bool = False
    synthetic_bgm: bool = False
    narration: bool = False
    workdir: Path | None = None
    note: str = (
        "Template assembler. The picture is the file you supplied "
        "or a generated test pattern. It is not text-to-video."
    )

    def to_json(self) -> dict:
        return {
            "output_name": self.output.name,
            "engine": self.engine,
            "duration": self.duration,
            "width": self.width,
            "height": self.height,
            "elapsed_sec": round(self.elapsed_sec, 3),
            "output_bytes": self.output_bytes,
            "warnings": self.warnings,
            "lines": [line.__dict__ for line in self.lines],
            "synthetic_video": self.synthetic_video,
            "synthetic_bgm": self.synthetic_bgm,
            "narration": self.narration,
            "note": self.note,
        }


def doctor() -> list[str]:
    """Return human-readable checks. Raise if ffmpeg is missing."""
    notes = []
    ffmpeg, ffprobe = _tools()
    notes.append(f"ffmpeg: {ffmpeg}")
    notes.append(f"ffprobe: {ffprobe}")
    if shutil.which("espeak-ng"):
        notes.append("espeak-ng: available (robotic Mandarin fallback)")
    else:
        notes.append("espeak-ng: MISSING (needed unless the Piper voice is installed)")
    font, fontsdir = _font()
    notes.append(f"subtitle font: {font} ({fontsdir})")
    try:
        engine = resolve_engine("auto")
        notes.append(f"default voice: {engine.name}")
    except TTSError as exc:
        notes.append(f"voice: {exc}")
    return notes


def build_video(
    *,
    output: Path,
    body: str = "",
    thing: str = "",
    other: str = "",
    video: Path | None = None,
    bgm: Path | None = None,
    color: str = "black",
    fontsize: int = 35,
    duration: float = BASE_DURATION,
    tts: str = "auto",
    narration: Path | None = None,
    synthetic_video: bool = False,
    synthetic_bgm: bool = False,
    pattern: str = "testsrc",
    workdir: Path | None = None,
    keep_temp: bool = False,
    timeout: int = 600,
    lines: list[Line] | None = None,
    silent: bool = False,
    bgm_volume_db: float = -20.0,
    subtitle_layout: str = "bottom",
    crf: int = 23,
) -> BuildResult:
    started = time.perf_counter()
    _tools()
    duration = _check_duration(duration)
    fontsize = _check_fontsize(fontsize)
    fill, outline = _colors(color)
    if subtitle_layout not in {"bottom", "card"}:
        raise PipelineError("subtitle layout must be bottom or card")
    if not 16 <= int(crf) <= 30:
        raise PipelineError("crf must be between 16 and 30")
    if not -40.0 <= float(bgm_volume_db) <= 6.0:
        raise PipelineError("background volume is out of range")
    warnings: list[str] = []
    custom = lines is not None
    if custom:
        try:
            lines = validate_lines(lines, duration)
        except ValueError as exc:
            raise PipelineError(str(exc)) from exc
        warnings.append(
            "Custom script. The eight-line Chinese template was not used."
        )
    else:
        lines = build_lines(body, thing, other, duration)
    if silent and narration is not None:
        raise PipelineError("silent mode does not take a narration file")

    if pattern not in {"testsrc", "gray"}:
        raise PipelineError("pattern must be testsrc or gray")

    job = workdir or (ROOT / "work" / "jobs" / uuid.uuid4().hex)
    job.mkdir(parents=True, exist_ok=True)
    os.chmod(job, 0o700)

    try:
        if video is None:
            if not synthetic_video:
                raise PipelineError("provide a video file or enable the synthetic test pattern")
            video = job / "synthetic-video.mp4"
            _synthetic_video(video, duration, pattern, timeout)
            warnings.append(
                "Picture is a generated test pattern (not footage, not text-to-video)."
            )
        else:
            video = _safe_media(video, "video")
            if synthetic_video:
                warnings.append("Using the uploaded video; synthetic picture was not requested as a replacement.")
        if bgm is None:
            if not synthetic_bgm:
                raise PipelineError("provide a background audio file or enable the generated tone")
            bgm = job / "synthetic-bgm.wav"
            _synthetic_tone(bgm, duration, timeout)
            warnings.append("Background audio is a generated sine tone, not music.")
        else:
            bgm = _safe_media(bgm, "background audio")
        if narration is not None:
            narration = _safe_media(narration, "narration")

        picture, width, height, fps = _prepare_picture(
            video, job / "picture.mp4", duration, timeout, warnings, crf=int(crf)
        )
        font, fontsdir = _font()
        ass_path = job / "subs.ass"
        ass_path.write_text(
            _ass(lines, width, height, font, fontsize, fill, outline, subtitle_layout),
            encoding="utf-8",
        )
        subtitled = job / "subtitled.mp4"
        _burn_subs(picture, ass_path, fontsdir, subtitled, fps, timeout, crf=int(crf))

        reports: list[LineReport] = []
        mix = job / "mix.wav"
        engine_name: str
        if silent:
            engine_name = "silent (no TTS)"
            warnings.append(
                "No speech. The audio is only the background score, at "
                f"{bgm_volume_db:.1f} dB."
            )
            _mix_score(bgm, mix, duration, float(bgm_volume_db), timeout)
            reports = [
                LineReport(line.text.replace("\n", " / "), round(line.start, 3), round(line.span, 3), 0.0, 1.0, False)
                for line in lines
            ]
        elif narration is not None:
            engine_name = "user narration file (not line-synced)"
            warnings.append(
                "Using the supplied narration file from the start of the video. "
                "It is not split onto the eight subtitle slots."
            )
            _mix_narration(bgm, narration, mix, duration, timeout)
        else:
            try:
                engine: Engine = resolve_engine(tts)
            except TTSError as exc:
                raise PipelineError(str(exc)) from exc
            engine_name = engine.name
            if engine_name.startswith("espeak"):
                warnings.append(
                    "Voice is espeak-ng Mandarin: robotic, and not the upstream iFlytek 小燕 voice. "
                    "Optional: python3 run.py --download-voice"
                )
            else:
                warnings.append(
                    "Voice is local Piper huayan, phonemized with espeak. "
                    "It is not the upstream iFlytek 小燕 voice, and it does not match that product."
                )
            spoken = [line for line in lines if line.kind != "mark"]
            voiced = _speak_lines(engine, spoken, job, reports, warnings)
            _mix_lines(bgm, voiced, reports, mix, duration, timeout)

        script_path = job / "script.txt"
        script_path.write_text(script_text(lines), encoding="utf-8")
        final_tmp = job / "final.mp4"
        _mux(subtitled, mix, final_tmp, timeout)
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.resolve() != final_tmp.resolve():
            shutil.copyfile(final_tmp, output)
            if not keep_temp:
                final_tmp.unlink()

        meta_src = job / "result.json"
        result = BuildResult(
            output=output,
            script_path=script_path,
            engine=engine_name,
            duration=duration,
            width=width,
            height=height,
            elapsed_sec=time.perf_counter() - started,
            output_bytes=output.stat().st_size,
            warnings=warnings,
            lines=reports,
            synthetic_video=any("generated test pattern" in item for item in warnings),
            synthetic_bgm=any("sine tone" in item for item in warnings),
            narration=narration is not None,
            workdir=job if keep_temp else None,
            note=(
                "Custom script composite through the local assembler. "
                "Not photorealistic text-to-video, and not a reach or virality claim."
                if custom
                else (
                    "Template assembler. The picture is the file you supplied "
                    "or a generated test pattern. It is not text-to-video."
                )
            ),
        )
        meta_src.write_text(json.dumps(result.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")
        if not keep_temp:
            # Keep the script and result next to a copy only via `output`.
            # The job directory still holds the rendered mp4 the web UI serves,
            # so the web UI passes keep_temp=True. CLI deletes heavy intermediates
            # but leaves script.txt and result.json for inspection.
            for name in (
                "picture.mp4",
                "subtitled.mp4",
                "mix.wav",
                "synthetic-video.mp4",
                "synthetic-bgm.wav",
            ):
                path = job / name
                if path.exists():
                    path.unlink()
            for path in job.glob("line-*.wav"):
                path.unlink()
        return result
    except Exception:
        if not keep_temp:
            # Leave the job directory on failure so the error can be diagnosed,
            # except we never want a partial final to be mistaken for success.
            partial = job / "final.mp4"
            if partial.exists() and not output.exists():
                partial.unlink()
        raise


def _check_duration(duration: float) -> float:
    try:
        duration = float(duration)
    except (TypeError, ValueError) as exc:
        raise PipelineError("duration must be a number of seconds") from exc
    if duration != duration or duration in {float("inf"), float("-inf")}:
        raise PipelineError("duration must be finite")
    if duration < MIN_DURATION or duration > MAX_DURATION:
        raise PipelineError(
            f"duration must be between {MIN_DURATION:.0f} and {MAX_DURATION:.0f} seconds "
            f"(upstream template is {BASE_DURATION:.0f}s)"
        )
    return duration


def _check_fontsize(fontsize: int) -> int:
    try:
        fontsize = int(fontsize)
    except (TypeError, ValueError) as exc:
        raise PipelineError("font size must be an integer") from exc
    if not 12 <= fontsize <= 96:
        raise PipelineError("font size must be between 12 and 96")
    return fontsize


def _colors(name: str) -> tuple[str, str]:
    raw = name.strip().lower()
    if raw.startswith("#"):
        raw = raw[1:]
    if raw in NAMED_COLORS:
        bgr = NAMED_COLORS[raw]
    elif len(raw) == 6 and all(ch in "0123456789abcdef" for ch in raw):
        bgr = raw[4:6] + raw[2:4] + raw[0:2]
    else:
        raise PipelineError(
            "color must be a name (black, white, yellow, red, blue, green, cyan, orange) "
            "or a hex RGB value"
        )
    r = int(bgr[4:6], 16)
    g = int(bgr[2:4], 16)
    b = int(bgr[0:2], 16)
    luminance = 0.299 * r + 0.587 * g + 0.114 * b
    outline = "FFFFFF" if luminance < 140 else "000000"
    return bgr, outline


def _tools() -> tuple[str, str]:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise PipelineError(
            "ffmpeg and ffprobe must be on PATH. "
            "The ffmpeg.exe and ffprobe.exe files shipped in this repo are not executed."
        )
    resolved = Path(ffmpeg).resolve()
    if resolved.name.lower() in {"ffmpeg.exe", "ffprobe.exe"} or resolved.parent == ROOT:
        raise PipelineError("refusing to execute a ffmpeg binary that lives in the repository")
    return ffmpeg, ffprobe


def _safe_media(path: Path, label: str) -> Path:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except FileNotFoundError as exc:
        raise PipelineError(f"{label} not found: {path}") from exc
    if not resolved.is_file():
        raise PipelineError(f"{label} is not a file")
    if resolved.stat().st_size <= 0:
        raise PipelineError(f"{label} is empty")
    if resolved.stat().st_size > 500 * 1024 * 1024:
        raise PipelineError(f"{label} is larger than 500 MB")
    return resolved


def _run(cmd: list[str], timeout: int) -> None:
    if not cmd or cmd[0].endswith(".exe"):
        raise PipelineError("refusing command")
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        _out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise PipelineError(f"{Path(cmd[0]).name} timed out after {timeout}s") from exc
    if proc.returncode != 0:
        detail = err.decode("utf-8", "replace").strip().splitlines()
        tail = " | ".join(detail[-6:])[:800]
        raise PipelineError(f"{Path(cmd[0]).name} failed: {tail}")


def _probe(path: Path) -> dict:
    _ffmpeg, ffprobe = _tools()
    proc = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        raise PipelineError(f"ffprobe could not read {path.name}: {proc.stderr.strip()[:300]}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise PipelineError(f"ffprobe returned unreadable data for {path.name}") from exc


def _stream(info: dict, kind: str) -> dict | None:
    for stream in info.get("streams") or []:
        if stream.get("codec_type") == kind:
            return stream
    return None


def _synthetic_video(dest: Path, duration: float, pattern: str, timeout: int) -> None:
    ffmpeg, _ffprobe = _tools()
    if pattern == "gray":
        source = "color=c=0x808080:s=640x360:r=24"
    else:
        source = "testsrc=size=640x360:rate=24"
    _run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            source,
            "-t",
            f"{duration:.3f}",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            str(dest),
        ],
        timeout,
    )


def _synthetic_tone(dest: Path, duration: float, timeout: int) -> None:
    ffmpeg, _ffprobe = _tools()
    _run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=196:sample_rate=44100:duration={duration:.3f}",
            "-c:a",
            "pcm_s16le",
            str(dest),
        ],
        timeout,
    )


def _prepare_picture(
    src: Path, dest: Path, duration: float, timeout: int, warnings: list[str], crf: int = 23
) -> tuple[Path, int, int, float]:
    info = _probe(src)
    video = _stream(info, "video")
    if video is None:
        raise PipelineError("input has no video stream")
    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    if width < 16 or height < 16:
        raise PipelineError("video frame size is too small")
    fps = _fps(video.get("avg_frame_rate") or video.get("r_frame_rate"))
    if fps < 5 or fps > 30:
        warnings.append(f"Frame rate {fps:.2f} was clamped to 24 for the local encode.")
        fps = 24.0
    even_w = width - (width % 2)
    even_h = height - (height % 2)
    scale = f"scale={even_w}:{even_h}"
    if width * height > MAX_PIXELS:
        warnings.append(
            f"Input is {width}x{height}, above 1920x1080. "
            "It is scaled to fit inside 1920x1080 so a local CPU can finish."
        )
        scale = "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2"
        even_w, even_h = 1920, 1080
    ffmpeg, _ffprobe = _tools()
    _run(
        [
            ffmpeg,
            "-y",
            "-stream_loop",
            "-1",
            "-i",
            str(src),
            "-t",
            f"{duration:.3f}",
            "-an",
            "-vf",
            f"{scale},setsar=1,format=yuv420p",
            "-r",
            f"{fps:.3f}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            str(int(crf)),
            str(dest),
        ],
        timeout,
    )
    if width * height <= MAX_PIXELS:
        width, height = even_w, even_h
    else:
        # pad filter makes a 1920x1080 frame even if the image is smaller inside it
        width, height = 1920, 1080
    return dest, width, height, fps


def _fps(value: str | None) -> float:
    if not value or value == "0/0":
        return 24.0
    if "/" in value:
        num, den = value.split("/", 1)
        try:
            den_f = float(den)
            if den_f == 0:
                return 24.0
            return float(num) / den_f
        except ValueError:
            return 24.0
    try:
        return float(value)
    except ValueError:
        return 24.0


def _font() -> tuple[str, str]:
    candidates = (
        "WenQuanYi Micro Hei",
        "Noto Sans CJK SC",
        "Noto Sans SC",
        "Droid Sans Fallback",
        "SimHei",
    )
    fc = shutil.which("fc-list")
    if not fc:
        raise PipelineError("fc-list is missing; install fontconfig and a CJK font")
    for family in candidates:
        proc = subprocess.run(
            [fc, family, "file"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        for line in proc.stdout.splitlines():
            path = line.split(":", 1)[0].strip()
            if path and Path(path).is_file():
                return family, str(Path(path).parent)
    raise PipelineError(
        "No CJK font found. Install fonts-wqy-microhei or fonts-noto-cjk."
    )


def _ass(
    lines: list[Line],
    width: int,
    height: int,
    font: str,
    fontsize: int,
    fill_bgr: str,
    outline_bgr: str,
    layout: str = "bottom",
) -> str:
    margin_v = 36 if layout == "bottom" else max(120, int(height * 0.12))
    if layout == "card":
        # White letters, heavy black edge, lower third. The box is the picture scrim.
        default_style = (
            f"Style: Default,{font},{fontsize},&H00FFFFFF,&H00000000,&H00000000,&H00000000,"
            f"0,0,0,0,100,100,0,0,1,6,0,2,78,78,{margin_v},1"
        )
    else:
        default_style = (
            f"Style: Default,{font},{fontsize},&H00{fill_bgr},&H00{fill_bgr},&H00{outline_bgr},&H64000000,"
            f"0,0,0,0,100,100,0,0,1,3,0,2,40,40,{margin_v},1"
        )
    mark_style = (
        f"Style: Mark,{font},26,&H00E4E4E4,&H00000000,&H00000000,&H00000000,"
        "0,0,0,0,100,100,0,0,1,2,0,8,48,48,34,1"
    )
    events = []
    for line in lines:
        style_name = "Mark" if line.kind == "mark" else "Default"
        text = _ass_escape(line.text) if line.kind == "mark" else _wrap_ass(line.text, width, fontsize)
        events.append(
            "Dialogue: 0,{start},{end},{style},,0,0,0,,{text}".format(
                start=_ass_time(line.start),
                end=_ass_time(line.start + line.span),
                style=style_name,
                text=text,
            )
        )
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
{default_style}
{mark_style}

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    return header + "\n".join(events) + "\n"


def _ass_time(seconds: float) -> str:
    if seconds < 0:
        seconds = 0
    total_cs = int(round(seconds * 100))
    seconds_part, cs = divmod(total_cs, 100)
    minutes, sec = divmod(seconds_part, 60)
    hours, minute = divmod(minutes, 60)
    return f"{hours}:{minute:02d}:{sec:02d}.{cs:02d}"


def _ass_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def _char_units(ch: str) -> float:
    if "\u2e80" <= ch <= "\u9fff" or "\uff00" <= ch <= "\uffef":
        return 1.0
    if ch == " ":
        return 0.33
    return 0.55


def _wrap_ass(text: str, width: int, fontsize: int) -> str:
    """Break lines libass would otherwise draw as one overflowing row.

    Newlines in the script are intentional phrase breaks. CJK glyphs count
    as full width; Latin glyphs count as a bit over half.
    """
    max_units = max(6.0, (width - 160) / max(fontsize, 1))
    rows: list[str] = []
    for para in text.split("\n"):
        buf = ""
        units = 0.0
        for ch in para:
            needed = _char_units(ch)
            if buf and units + needed > max_units:
                rows.append(buf.rstrip())
                buf = ""
                units = 0.0
                if ch == " ":
                    continue
            buf += ch
            units += needed
        if buf.strip():
            rows.append(buf.strip())
    if not rows:
        rows = [text]
    return r"\N".join(_ass_escape(row) for row in rows)


def _filter_path(path: str) -> str:
    return (
        path.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace(",", "\\,")
    )


def _burn_subs(
    picture: Path, ass_path: Path, fontsdir: str, dest: Path, fps: float, timeout: int, crf: int = 23
) -> None:
    ffmpeg, _ffprobe = _tools()
    # Run in the job directory so the filter graph only names subs.ass,
    # never the subtitle sentence.
    _run_cwd(
        [
            ffmpeg,
            "-y",
            "-i",
            picture.name,
            "-vf",
            f"ass={ass_path.name}:fontsdir={_filter_path(fontsdir)}",
            "-r",
            f"{fps:.3f}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            str(int(crf)),
            "-pix_fmt",
            "yuv420p",
            dest.name,
        ],
        picture.parent,
        timeout,
    )


def _run_cwd(cmd: list[str], cwd: Path, timeout: int) -> None:
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        _out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise PipelineError(f"{Path(cmd[0]).name} timed out after {timeout}s") from exc
    if proc.returncode != 0:
        detail = err.decode("utf-8", "replace").strip().splitlines()
        tail = " | ".join(detail[-8:])[:800]
        raise PipelineError(f"{Path(cmd[0]).name} failed: {tail}")


def _speak_lines(
    engine: Engine,
    lines: list[Line],
    job: Path,
    reports: list[LineReport],
    warnings: list[str],
) -> list[Path]:
    paths = []
    for index, line in enumerate(lines):
        dest = job / f"line-{index}.wav"
        try:
            engine.synthesize(line.text, dest)
        except TTSError as exc:
            raise PipelineError(str(exc)) from exc
        spoken = wav_duration(dest)
        ratio = spoken / line.span if line.span > 0 else 99.0
        atempo = 1.0
        trimmed = False
        if ratio > 1.02:
            atempo = min(ratio, 2.5)
            trimmed = ratio > 2.5
            if trimmed:
                warnings.append(
                    f"Line {index + 1} is longer than its slot even at 2.5x and was trimmed."
                )
            elif ratio > 1.25:
                warnings.append(f"Line {index + 1} was sped up {atempo:.2f}x to fit its slot.")
        reports.append(
            LineReport(
                text=line.text,
                start=round(line.start, 3),
                span=round(line.span, 3),
                speech_sec=round(spoken, 3),
                atempo=round(atempo, 3),
                trimmed=trimmed,
            )
        )
        paths.append(dest)
    return paths


def _mix_lines(
    bgm: Path,
    voices: list[Path],
    reports: list[LineReport],
    dest: Path,
    duration: float,
    timeout: int,
) -> None:
    ffmpeg, _ffprobe = _tools()
    cmd = [ffmpeg, "-y", "-stream_loop", "-1", "-i", str(bgm)]
    for voice in voices:
        cmd.extend(["-i", str(voice)])
    parts = [
        f"[0:a]aformat=sample_rates=44100:channel_layouts=stereo,volume=-20dB,"
        f"atrim=0:{duration:.3f},apad=whole_dur={duration:.3f},asetpts=PTS-STARTPTS[bgm]"
    ]
    labels = ["[bgm]"]
    for index, report in enumerate(reports):
        chain = f"[{index + 1}:a]aformat=sample_rates=44100:channel_layouts=mono"
        if report.atempo > 1.01:
            chain += f",atempo={report.atempo:.4f}"
        delay = int(round(report.start * 1000))
        chain += (
            f",apad=whole_dur={report.span:.3f},atrim=0:{report.span:.3f},"
            f"aformat=channel_layouts=stereo,adelay={delay}|{delay},asetpts=PTS-STARTPTS[v{index}]"
        )
        parts.append(chain)
        labels.append(f"[v{index}]")
    count = len(labels)
    parts.append(
        f"{''.join(labels)}amix=inputs={count}:duration=first:dropout_transition=0:normalize=0[a]"
    )
    cmd.extend(
        [
            "-filter_complex",
            ";".join(parts),
            "-map",
            "[a]",
            "-t",
            f"{duration:.3f}",
            "-c:a",
            "pcm_s16le",
            str(dest),
        ]
    )
    _run(cmd, timeout)


def _mix_narration(bgm: Path, narration: Path, dest: Path, duration: float, timeout: int) -> None:
    info = _probe(narration)
    if _stream(info, "audio") is None:
        raise PipelineError("narration file has no audio stream")
    ffmpeg, _ffprobe = _tools()
    _run(
        [
            ffmpeg,
            "-y",
            "-stream_loop",
            "-1",
            "-i",
            str(bgm),
            "-i",
            str(narration),
            "-filter_complex",
            (
                f"[0:a]aformat=sample_rates=44100:channel_layouts=stereo,volume=-20dB,"
                f"atrim=0:{duration:.3f},asetpts=PTS-STARTPTS[bgm];"
                f"[1:a]aformat=sample_rates=44100:channel_layouts=stereo,"
                f"atrim=0:{duration:.3f},apad=whole_dur={duration:.3f},asetpts=PTS-STARTPTS[nar];"
                f"[bgm][nar]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]"
            ),
            "-map",
            "[a]",
            "-t",
            f"{duration:.3f}",
            "-c:a",
            "pcm_s16le",
            str(dest),
        ],
        timeout,
    )


def _mix_score(bgm: Path, dest: Path, duration: float, volume_db: float, timeout: int) -> None:
    ffmpeg, _ffprobe = _tools()
    _run(
        [
            ffmpeg,
            "-y",
            "-stream_loop",
            "-1",
            "-i",
            str(bgm),
            "-af",
            (
                f"volume={volume_db:.2f}dB,atrim=0:{duration:.3f},"
                f"apad=whole_dur={duration:.3f},aformat=sample_rates=44100:channel_layouts=stereo"
            ),
            "-t",
            f"{duration:.3f}",
            "-c:a",
            "pcm_s16le",
            str(dest),
        ],
        timeout,
    )


def _mux(video: Path, audio: Path, dest: Path, timeout: int) -> None:
    ffmpeg, _ffprobe = _tools()
    _run(
        [
            ffmpeg,
            "-y",
            "-i",
            str(video),
            "-i",
            str(audio),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-movflags",
            "+faststart",
            "-shortest",
            str(dest),
        ],
        timeout,
    )
