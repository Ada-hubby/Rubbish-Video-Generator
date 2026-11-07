"""Original silent-film demo rendered by the local assembler.

Window Seat is a fictional Taglish story. The picture is procedural
vector-style art: silhouettes, a bus window, rain. No photographs, no real
people, no chat UI. There is no speech. The score is synthesized in-process
(a minor pad, rain noise, and two single bells). It is not a commercial
track, and nothing here is a claim about views.

The eight-line Chinese template is not used. Cards are a custom script
passed into ``build_video``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from .pipeline import ROOT, PipelineError, _tools, build_video
from .script import Line, script_text

WIDTH = 720
HEIGHT = 1280
FPS = 24
DURATION = 32.0

# Cue times are the card starts. Animation reads the same numbers.
HOOK = 0.0
PLANT = 3.4
SAVE = 8.0
MOVE = 12.6
STAND = 17.4
TWIST = 22.2
BUTTON = 27.6


def story_lines() -> list[Line]:
    cards = (
        (HOOK, PLANT, "Huwag kang lumingon."),
        (PLANT, SAVE, "Window seat.\nShe gets dizzy, eh."),
        (SAVE, MOVE, "Aisle seat saved.\nPara sa kanya."),
        (MOVE, STAND, "The bus moved.\nWalang umupo."),
        (STAND, TWIST, "She stood up.\nLast stop."),
        (TWIST, BUTTON, "From behind:\nKeep the window.\nAndito na ako."),
        (BUTTON, DURATION, "Hindi siya nawala."),
    )
    lines = [Line(text, end - start, start, "card") for start, end, text in cards]
    lines.append(Line("Fictional story", DURATION, 0.0, "mark"))
    return lines


def story_script() -> str:
    header = (
        "Window Seat\n"
        "Original fictional silent film. Taglish cards. No real people.\n"
        "Picture: procedural silhouettes. Score: synthesized pad, rain, bells.\n"
        "Not the Chinese marketing template. Not a virality claim.\n\n"
    )
    return header + script_text(story_lines())


def build_demo(output: Path) -> object:
    """Draw the picture, synthesize the score, burn cards through the assembler."""
    output = Path(output)
    work = ROOT / "work" / "drama-src"
    work.mkdir(parents=True, exist_ok=True)
    picture = work / "window-seat-picture.mp4"
    score = work / "window-seat-score.wav"
    _render_picture(picture)
    _render_score(score)
    return build_video(
        output=output,
        video=picture,
        bgm=score,
        lines=story_lines(),
        silent=True,
        bgm_volume_db=-1.0,
        subtitle_layout="card",
        fontsize=48,
        color="white",
        duration=DURATION,
        crf=18,
        timeout=600,
    )


def _smooth(a: float, b: float, t: float) -> float:
    if t <= a:
        return 0.0
    if t >= b:
        return 1.0
    x = (t - a) / (b - a)
    return x * x * (3.0 - 2.0 * x)


def _background() -> np.ndarray:
    y = np.linspace(0.0, 1.0, HEIGHT)[:, None]
    top = np.array([16, 22, 46], dtype=np.float32)
    bot = np.array([7, 8, 16], dtype=np.float32)
    grad = top * (1.0 - y) + bot * y
    grad = np.repeat(grad[:, None, :], WIDTH, axis=1)
    yy = np.linspace(-1.0, 1.0, HEIGHT)[:, None]
    xx = np.linspace(-1.0, 1.0, WIDTH)[None, :]
    vig = np.clip(1.0 - 0.42 * (xx ** 2 + (yy * 0.72) ** 2), 0.55, 1.0)
    return np.clip(grad * vig[..., None], 0, 255).astype(np.uint8)


def _drops() -> np.ndarray:
    rng = np.random.default_rng(11)
    return rng.random((96, 3))


_BG = None
_DROPS = None


def _render_picture(dest: Path) -> None:
    global _BG, _DROPS
    _BG = _background()
    _DROPS = _drops()
    ffmpeg, _ffprobe = _tools()
    frames = int(round(DURATION * FPS))
    proc = subprocess.Popen(
        [
            ffmpeg,
            "-y",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-s",
            f"{WIDTH}x{HEIGHT}",
            "-r",
            str(FPS),
            "-i",
            "-",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            str(dest),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    assert proc.stdin is not None and proc.stderr is not None
    try:
        for index in range(frames):
            frame = _frame(index / FPS)
            proc.stdin.write(frame)
    except BrokenPipeError:
        err = proc.stderr.read().decode("utf-8", "replace")[-600:]
        raise PipelineError(f"picture encode failed: {err}") from None
    proc.stdin.close()
    err = proc.stderr.read().decode("utf-8", "replace")
    if proc.wait() != 0:
        raise PipelineError(f"picture encode failed: {err[-600:]}")


def _frame(t: float) -> bytes:
    assert _BG is not None and _DROPS is not None
    image = Image.fromarray(_BG, "RGB").convert("RGBA")
    overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    stand = _smooth(STAND, STAND + 2.6, t) * (1.0 - 0.45 * _smooth(BUTTON, BUTTON + 2.0, t))
    him = _smooth(TWIST, TWIST + 1.6, t)
    turn = _smooth(BUTTON, BUTTON + 2.0, t)
    glow = 0.35 + 0.65 * (1.0 - him) * _smooth(SAVE, SAVE + 1.2, t)
    move = _smooth(MOVE, MOVE + 1.5, t)

    # Window showing the night the bus is driving through.
    draw.rounded_rectangle((58, 150, 662, 690), radius=28, fill=(18, 32, 58, 255))
    _lights(draw, t, move)
    _rain(draw, t, strong=0.55 + 0.45 * move)
    draw.rounded_rectangle((58, 150, 662, 690), radius=28, outline=(210, 186, 150, 180), width=8)
    draw.line((360, 158, 360, 682), fill=(210, 186, 150, 90), width=4)
    # A small practical lamp. It warms up when the second figure arrives.
    draw.ellipse((78, 168, 196, 286), fill=(255, 196, 120, int(50 + 80 * him)))

    # Interior. She sits in the near row. He is the smaller row behind.
    draw.rounded_rectangle((40, 700, 680, 1040), radius=18, fill=(18, 14, 20, 235))
    draw.line((80, 760, 80, 1000), fill=(186, 160, 120, 120), width=6)
    _seat(draw, 120, 860, 200, 78, (62, 32, 40, 255))
    _seat(
        draw,
        390,
        860,
        200,
        78,
        (42, 24, 32, 255),
        outline=(242, 196, 130, int(90 + 140 * glow)),
        glow=glow,
    )
    if him > 0.02:
        _seat(draw, 250, 748, 160, 46, (40, 26, 34, int(200 * him)))

    _person(draw, 210, 900, 1.05, stand, facing=1.0 - 1.6 * turn, fill=(12, 10, 14, 255), hair=1.15)
    if him > 0.02:
        alpha = int(255 * him)
        _person(
            draw, 310, 790, 0.72, rise=0.15, facing=-0.2, fill=(10, 9, 12, alpha), hair=0.55
        )
        # Arm toward the back of her seat, so "behind" is a physical gesture.
        draw.line(
            (330, 760, 250, 860),
            fill=(10, 9, 12, alpha),
            width=14,
        )

    # Lower scrim so the burned-in cards sit on one dark field.
    scrim = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    ImageDraw.Draw(scrim).rectangle((0, 980, WIDTH, HEIGHT), fill=(5, 5, 8, 210))
    image = Image.alpha_composite(image, overlay)
    image = Image.alpha_composite(image, scrim)
    rgb = image.convert("RGB")
    return rgb.tobytes()


def _lights(draw: ImageDraw.ImageDraw, t: float, move: float) -> None:
    speed = 40 + 280 * move
    colors = (
        (255, 214, 150, 210),
        (170, 210, 255, 180),
        (255, 244, 220, 200),
        (255, 150, 110, 170),
    )
    for i in range(16):
        base = (i * 97) % (WIDTH + 240)
        x = (base - t * speed) % (WIDTH + 240) - 120
        y = 190 + (i * 53) % 430
        if 80 < x < 630 and 180 < y < 650:
            color = colors[i % 4]
            draw.rounded_rectangle((x, y, x + 54, y + 8), radius=3, fill=color)


def _rain(draw: ImageDraw.ImageDraw, t: float, strong: float) -> None:
    assert _DROPS is not None
    alpha = int(70 + 80 * strong)
    color = (186, 206, 224, alpha)
    for drop in _DROPS:
        x = drop[0] * WIDTH
        y = ((drop[1] + t * (0.35 + drop[2] * 0.8)) % 1.0) * 760 + 140
        length = 16 + drop[2] * 28
        draw.line((x, y, x - 6, y + length), fill=color, width=2)


def _seat(draw, x, y, w, h, fill, outline=None, glow: float = 0.0) -> None:
    if glow > 0:
        pad = int(8 + 6 * glow)
        draw.rounded_rectangle(
            (x - pad, y - pad, x + w + pad, y + h + pad),
            radius=16,
            fill=(242, 186, 110, int(50 * glow)),
        )
    if outline is None:
        draw.rounded_rectangle((x, y, x + w, y + h), radius=12, fill=fill)
    else:
        draw.rounded_rectangle((x, y, x + w, y + h), radius=12, fill=fill, outline=outline, width=4)


def _person(draw, cx, hip, scale, rise, facing, fill, hair: float) -> None:
    s = scale
    head_y = hip - (168 + rise * 78) * s
    head_x = cx + facing * 70 * s
    r = 34 * s
    shoulder_y = head_y + r + 10 * s
    draw.polygon(
        [
            (cx - 58 * s, hip),
            (cx + 54 * s, hip - rise * 16 * s),
            (cx + 46 * s, shoulder_y),
            (cx - 50 * s, shoulder_y),
        ],
        fill=fill,
    )
    draw.ellipse((head_x - r, head_y - r, head_x + r, head_y + r), fill=fill)
    # Hair mass only. No eyes, no mouth, no likeness.
    draw.ellipse(
        (head_x - r * hair, head_y - r * 1.05, head_x + r * 0.25, head_y + r * 0.9),
        fill=fill,
    )


def _render_score(dest: Path) -> None:
    sr = 44100
    n = int(DURATION * sr)
    t = np.arange(n, dtype=np.float32) / sr
    rng = np.random.default_rng(3)
    noise = rng.standard_normal(n).astype(np.float32)
    rain = np.diff(noise, prepend=noise[0])
    rain *= 0.018 * (0.45 + 0.55 * _envelope(t, MOVE, MOVE + 2.0))

    def partial(freq: float, amp: float) -> np.ndarray:
        wobble = 1.0 + 0.004 * np.sin(2 * np.pi * 0.07 * t)
        return amp * np.sin(2 * np.pi * freq * wobble * t)

    lonely = partial(110.0, 0.05) + partial(164.81, 0.035) + partial(261.63, 0.028)
    lift = partial(130.81, 0.045) + partial(164.81, 0.03) + partial(196.0, 0.026) + partial(329.63, 0.018)
    opened = _envelope(t, TWIST, TWIST + 2.2)
    pad = lonely * (1.0 - opened) + lift * opened
    pad *= 0.85 + 0.15 * np.sin(2 * np.pi * 0.12 * t)

    pulse_rate = 1.15
    pulse = (0.5 + 0.5 * np.sin(2 * np.pi * pulse_rate * t)) ** 8
    pulse = pulse * partial(55.0, 0.04) * (1.0 - 0.55 * opened)

    bell_1 = _bell(t, TWIST + 0.35, 523.25, 0.07)
    bell_2 = _bell(t, BUTTON + 0.2, 659.25, 0.045)
    mix = rain + pad + pulse + bell_1 + bell_2
    peak = float(np.max(np.abs(mix))) or 1.0
    mix = mix / peak * 0.34
    stereo = np.column_stack((mix * 0.98, mix)).astype(np.float32)
    pcm = np.clip(stereo * 32767.0, -32767, 32767).astype(np.int16)
    import wave

    with wave.open(str(dest), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(sr)
        handle.writeframes(pcm.tobytes())


def _envelope(t: np.ndarray, a: float, b: float) -> np.ndarray:
    x = np.clip((t - a) / max(b - a, 1e-6), 0.0, 1.0)
    return (x * x * (3.0 - 2.0 * x)).astype(np.float32)


def _bell(t: np.ndarray, when: float, freq: float, amp: float) -> np.ndarray:
    dt = np.clip(t - when, 0.0, None)
    gate = (t >= when).astype(np.float32)
    return (amp * np.sin(2 * np.pi * freq * t) * np.exp(-3.2 * dt) * gate).astype(np.float32)
