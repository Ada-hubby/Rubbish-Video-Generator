"""Offline speech. No iFlytek, no cloud TTS, no transcript upload.

Engines
-------
espeak-ng
    Always local. Robotic Mandarin. Works with no model download.
piper zh_CN-huayan-medium
    Local ONNX voice from the MIT-licensed rhasspy/piper-voices repo.
    About 61 MB, downloaded only when you run ``--download-voice``.
    This voice's phoneme frontend is espeak. It is not iFlytek 小燕.
    Piper voices whose config would pull an extra g2p model are refused,
    so synthesis cannot open a network connection by surprise.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import urllib.request
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODEL_DIR = ROOT / "models"
MODEL_ONNX = MODEL_DIR / "zh_CN-huayan-medium.onnx"
MODEL_JSON = MODEL_DIR / "zh_CN-huayan-medium.onnx.json"

ONNX_URL = (
    "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
    "zh/zh_CN/huayan/medium/zh_CN-huayan-medium.onnx"
)
JSON_URL = ONNX_URL + ".json"
ONNX_SHA256 = "9929917bf8cabb26fd528ea44d3a6699c11e87317a14765312420be230be0f3d"
JSON_SHA256 = "d521dc45504a8ccc99e325822b35946dd701840bfb07e3dbb31a40929ed6a82b"
ONNX_BYTES = 63_201_294

# Hugging Face hosts the file. Nothing from the user's script is sent.
DOWNLOAD_NOTE = (
    "Optional voice weights: zh_CN-huayan-medium (~60 MB) from "
    "huggingface.co/rhasspy/piper-voices (MIT). SHA-256 " + ONNX_SHA256
)


class TTSError(RuntimeError):
    pass


class Engine:
    name = "unset"

    def synthesize(self, text: str, dest: Path) -> None:
        raise NotImplementedError


class EspeakEngine(Engine):
    name = "espeak-ng (cmn, robotic)"

    def __init__(self) -> None:
        binary = shutil.which("espeak-ng")
        if not binary:
            raise TTSError(
                "espeak-ng is not on PATH. Install the espeak-ng package, "
                "or install the optional Piper voice (python3 run.py --download-voice)."
            )
        self.binary = binary

    def synthesize(self, text: str, dest: Path) -> None:
        # argv only; the line is not a shell command.
        proc = subprocess.run(
            [self.binary, "-v", "cmn", "-s", "160", "-w", str(dest), text],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if proc.returncode != 0 or not dest.is_file() or dest.stat().st_size < 800:
            detail = (proc.stderr or proc.stdout or "").strip()[:400]
            raise TTSError(f"espeak-ng failed to synthesize a line. {detail}")


_PIPER_VOICE = None


class PiperEngine(Engine):
    name = "piper zh_CN-huayan-medium (local ONNX, espeak phonemes)"

    def __init__(self, model: Path = MODEL_ONNX, config: Path = MODEL_JSON) -> None:
        if not model.is_file() or not config.is_file():
            raise TTSError(
                "Piper voice files are not in models/. "
                "Run: python3 run.py --download-voice\n" + DOWNLOAD_NOTE
            )
        _assert_espeak_phonemes(config)
        self.model = model
        self.config = config

    def synthesize(self, text: str, dest: Path) -> None:
        voice = _load_piper(self.model, self.config)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(dest), "wb") as handle:
            voice.synthesize_wav(text, handle)
        if dest.stat().st_size < 800:
            raise TTSError("Piper wrote an empty wav")


def _assert_espeak_phonemes(config: Path) -> None:
    data = json.loads(config.read_text(encoding="utf-8"))
    kind = data.get("phoneme_type")
    if kind != "espeak":
        raise TTSError(
            f"Refusing voice config phoneme_type={kind!r}. "
            "Piper would download an extra phonemizer model for that type."
        )


def _load_piper(model: Path, config: Path):
    global _PIPER_VOICE
    if _PIPER_VOICE is not None:
        return _PIPER_VOICE
    try:
        from piper import PiperVoice
    except ImportError as exc:
        raise TTSError(
            "piper-tts is not installed. pip install -r requirements-voice.txt"
        ) from exc
    # download_dir is set so a future code path cannot drop files in the repo root.
    # use_cuda is forced off: this port does not require a GPU.
    _PIPER_VOICE = PiperVoice.load(
        model,
        config_path=config,
        use_cuda=False,
        download_dir=MODEL_DIR,
    )
    return _PIPER_VOICE


def piper_ready() -> bool:
    if not MODEL_ONNX.is_file() or not MODEL_JSON.is_file():
        return False
    try:
        _assert_espeak_phonemes(MODEL_JSON)
    except TTSError:
        return False
    try:
        import piper  # noqa: F401
    except ImportError:
        return False
    return True


def resolve_engine(choice: str) -> Engine:
    choice = (choice or "auto").lower()
    if choice not in {"auto", "espeak", "piper"}:
        raise TTSError("tts must be auto, espeak, or piper")
    if choice == "espeak":
        return EspeakEngine()
    if choice == "piper":
        return PiperEngine()
    if piper_ready():
        return PiperEngine()
    return EspeakEngine()


def download_voice(dest_dir: Path = MODEL_DIR) -> Path:
    """Download the pinned voice. Called only from the explicit CLI flag."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    _fetch(ONNX_URL, dest_dir / MODEL_ONNX.name, ONNX_SHA256, ONNX_BYTES)
    _fetch(JSON_URL, dest_dir / MODEL_JSON.name, JSON_SHA256, None)
    _assert_espeak_phonemes(dest_dir / MODEL_JSON.name)
    return dest_dir / MODEL_ONNX.name


def _fetch(url: str, dest: Path, expected_sha256: str, expected_size: int | None) -> None:
    tmp = dest.with_suffix(dest.suffix + ".partial")
    if tmp.exists():
        tmp.unlink()
    request = urllib.request.Request(url, headers={"User-Agent": "rubbish-video-local/2"})
    digest = hashlib.sha256()
    size = 0
    try:
        with urllib.request.urlopen(request, timeout=120) as response, tmp.open("wb") as handle:
            while True:
                chunk = response.read(1024 * 256)
                if not chunk:
                    break
                handle.write(chunk)
                digest.update(chunk)
                size += len(chunk)
                if size > 80 * 1024 * 1024:
                    raise TTSError("voice download exceeded 80 MB; aborted")
    except TTSError:
        tmp.unlink(missing_ok=True)
        raise
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise TTSError(f"could not download voice file: {exc}") from exc
    if expected_size is not None and size != expected_size:
        tmp.unlink(missing_ok=True)
        raise TTSError(f"voice download size {size} != expected {expected_size}")
    got = digest.hexdigest()
    if got != expected_sha256:
        tmp.unlink(missing_ok=True)
        raise TTSError("voice download SHA-256 did not match the pinned hash; file deleted")
    tmp.replace(dest)


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        if rate <= 0:
            raise TTSError(f"bad wav sample rate: {path.name}")
        return handle.getnframes() / float(rate)
