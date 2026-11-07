"""Local assembler tests. They generate synthetic media only."""

from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from rubbish_video.pipeline import PipelineError, build_video, doctor
from rubbish_video.script import BASE_SLOTS, build_lines, load_args_file
from rubbish_video.webui import JOBS, App, LocalOnlyServer, _Limited, parse_multipart

ROOT = Path(__file__).resolve().parents[1]
SECRET = "super-secret-appid;super-secret-key;super-secret-value"


class ScriptTests(unittest.TestCase):
    def test_upstream_slots_at_50_seconds(self) -> None:
        lines = build_lines("核桃核", "不能吞下去", "核桃核太大了，吞下去容易噎着", 50)
        self.assertEqual(len(lines), 8)
        self.assertEqual([(line.span, line.start) for line in lines], [(float(s), float(t)) for s, t in BASE_SLOTS])
        self.assertEqual(lines[0].text, "核桃核不能吞下去是怎么回事呢？")
        self.assertIn("其实就是核桃核太大了", lines[3].text)

    def test_scales_shorter_duration(self) -> None:
        lines = build_lines("甲", "乙", "丙", 25)
        self.assertAlmostEqual(lines[1].start, 2.0)
        self.assertAlmostEqual(lines[1].span, 3.5)

    def test_long_subtitle_wraps_on_a_narrow_frame(self) -> None:
        from rubbish_video.pipeline import _wrap_ass

        text = "核桃核不能吞下去，其实就是核桃核太大了，吞下去容易噎着"
        wrapped = _wrap_ass(text, 640, 32)
        self.assertIn(r"\N", wrapped)
        self.assertTrue(wrapped.replace(r"\N", "").startswith("核桃核"))

    def test_rejects_control_characters(self) -> None:
        with self.assertRaises(ValueError):
            build_lines("ok", "bad\nline", "other", 50)

    def test_args_file_does_not_keep_a_usable_secret(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "args.txt"
            path.write_text(
                "核桃核\n不能吞下去\n太大了\n" + SECRET + "\nblack\n35\n",
                encoding="utf-8",
            )
            loaded = load_args_file(path)
        self.assertTrue(loaded.ignored_legacy_credentials)
        self.assertEqual(loaded.body, "核桃核")
        self.assertNotIn("super-secret", loaded.body)
        blob = json.dumps(loaded.__dict__)
        self.assertNotIn("super-secret", blob)


class ParserTests(unittest.TestCase):
    def test_multipart_text_and_file(self) -> None:
        boundary = "----testboundary"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="body"\r\n\r\n'
            "核桃核\r\n"
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="video"; filename="clip.mp4"\r\n'
            "Content-Type: video/mp4\r\n\r\n"
        ).encode() + b"\x00\x01fake-not-a-real-mp4" + f"\r\n--{boundary}--\r\n".encode()
        with tempfile.TemporaryDirectory() as tmp:
            fields, files = parse_multipart(_Limited(_Raw(body), len(body)), boundary.encode(), Path(tmp))
            self.assertEqual(fields["body"], "核桃核")
            self.assertEqual(files["video"].read_bytes(), b"\x00\x01fake-not-a-real-mp4")


class AssemblerTests(unittest.TestCase):
    def test_doctor(self) -> None:
        notes = "\n".join(doctor())
        self.assertIn("ffmpeg", notes)
        self.assertIn("subtitle font", notes)

    def test_gray_clip_burns_subtitle_and_has_audio(self) -> None:
        marker = Path("/tmp/rubbish-pwned-by-phrase")
        if marker.exists():
            marker.unlink()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output = tmp_path / "out.mp4"
            result = build_video(
                output=output,
                body="$(touch /tmp/rubbish-pwned-by-phrase)",
                thing="不能吞下去",
                other="只是测试",
                duration=8,
                tts="espeak",
                synthetic_video=True,
                synthetic_bgm=True,
                pattern="gray",
                color="black",
                fontsize=28,
                keep_temp=True,
            )
            self.assertFalse(marker.exists(), "phrase was executed in a shell")
            self.assertTrue(output.is_file())
            self.assertGreater(output.stat().st_size, 10_000)
            info = _ffprobe(output)
            kinds = {stream["codec_type"] for stream in info["streams"]}
            self.assertEqual(kinds, {"video", "audio"})
            duration = float(info["format"]["duration"])
            self.assertAlmostEqual(duration, 8.0, delta=0.4)
            self.assertIn("espeak", result.engine)
            self.assertNotIn("xfyun", result.script_path.read_text(encoding="utf-8"))
            raw = _raw_frame(output, 0.4)
            self.assertGreater(_band_delta(raw, 640, 360, 0.78, 1.0), 0.01)
            self.assertLess(_band_delta(raw, 640, 360, 0.0, 0.45), 0.005)
            script = result.script_path.read_text(encoding="utf-8")
            self.assertIn("不能吞下去是怎么回事呢", script)

    def test_original_50_second_timeline_with_espeak(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "full.mp4"
            result = build_video(
                output=output,
                body="核桃核",
                thing="不能吞下去",
                other="核桃核太大了，吞下去容易噎着",
                duration=50,
                tts="espeak",
                synthetic_video=True,
                synthetic_bgm=True,
                pattern="testsrc",
                keep_temp=False,
            )
            info = _ffprobe(output)
            self.assertAlmostEqual(float(info["format"]["duration"]), 50.0, delta=0.5)
            self.assertTrue(any(line.start == 0 for line in result.lines))
            self.assertTrue(any(abs(line.start - 40) < 0.01 for line in result.lines))
            volume = _mean_volume(output)
            self.assertGreater(volume, -45.0, f"audio looks silent: {volume} dB")

    def test_user_files_and_narration_replace_tts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            video = folder / "clip.mp4"
            bgm = folder / "bed.wav"
            narration = folder / "narration.wav"
            output = folder / "out.mp4"
            subprocess.run(
                ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=0x336699:s=320x240:r=15", "-t", "8", "-pix_fmt", "yuv420p", str(video)],
                check=True, capture_output=True,
            )
            subprocess.run(
                ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=220:duration=3", "-c:a", "pcm_s16le", str(bgm)],
                check=True, capture_output=True,
            )
            subprocess.run(
                ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=880:duration=8", "-c:a", "pcm_s16le", str(narration)],
                check=True, capture_output=True,
            )
            result = build_video(
                output=output,
                video=video,
                bgm=bgm,
                narration=narration,
                body="甲",
                thing="乙",
                other="丙",
                duration=8,
                tts="espeak",
                fontsize=24,
                color="yellow",
            )
            self.assertTrue(result.narration)
            self.assertIn("narration", result.engine)
            info = _ffprobe(output)
            self.assertAlmostEqual(float(info["format"]["duration"]), 8.0, delta=0.4)
            self.assertEqual(info["streams"][0]["width"], 320)


class WebTests(unittest.TestCase):
    def test_server_is_loopback_and_renders(self) -> None:
        server = LocalOnlyServer(("127.0.0.1", 0), App)
        host, port = server.server_address
        self.assertEqual(host, "127.0.0.1")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            home = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10)
            page = home.read().decode()
            self.assertIn("does", page.lower())
            self.assertIn("not", page)
            self.assertIn("iFlytek", page)
            boundary = "----webtest"
            payload = (
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="body"\r\n\r\n甲\r\n'
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="thing"\r\n\r\n乙\r\n'
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="other"\r\n\r\n丙\r\n'
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="duration"\r\n\r\n8\r\n'
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="tts"\r\n\r\nespeak\r\n'
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="synthetic_video"\r\n\r\non\r\n'
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="synthetic_bgm"\r\n\r\non\r\n'
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="color"\r\n\r\nwhite\r\n'
                f"--{boundary}--\r\n"
            ).encode()
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/generate",
                data=payload,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                method="POST",
            )
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, headers, newurl):
                    return None

            opener = urllib.request.build_opener(NoRedirect)
            try:
                opener.open(request, timeout=180)
                self.fail("expected redirect")
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 303)
                location = exc.headers["Location"]
            self.assertTrue(location.startswith("/jobs/"))
            job_id = location.rsplit("/", 1)[-1]
            self.assertTrue((JOBS / job_id / "final.mp4").is_file())
            video = urllib.request.urlopen(f"http://127.0.0.1:{port}{location}/final.mp4", timeout=30)
            blob = video.read(32)
            self.assertTrue(blob.startswith(b"\x00\x00\x00") or b"ftyp" in blob or len(blob) > 8)
            result_page = urllib.request.urlopen(f"http://127.0.0.1:{port}{location}", timeout=10)
            html_page = result_page.read().decode()
            self.assertIn("甲乙是怎么回事呢", html_page)
            self.assertIn("espeak", html_page)
        finally:
            server.shutdown()
            server.server_close()


class SourceGuardTests(unittest.TestCase):
    def test_paid_tts_endpoint_is_gone(self) -> None:
        banned = ("tts-api.xfyun.cn", "ws-api.xfyun.cn", "websocket.WebSocketApp", "CERT_NONE")
        paths = [ROOT / "run.py", *(ROOT / "rubbish_video").rglob("*.py")]
        for path in paths:
            text = path.read_text(encoding="utf-8")
            for token in banned:
                self.assertNotIn(token, text, f"{token} still in {path}")


class _Raw:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.i = 0

    def read(self, n: int) -> bytes:
        chunk = self.data[self.i : self.i + n]
        self.i += len(chunk)
        return chunk


def _ffprobe(path: Path) -> dict:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(proc.stdout)


def _raw_frame(path: Path, timestamp: float) -> bytes:
    proc = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-ss",
            f"{timestamp}",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ],
        check=True,
        capture_output=True,
    )
    return proc.stdout


def _band_delta(raw: bytes, width: int, height: int, y0: float, y1: float) -> float:
    row = width * 3
    start = int(height * y0)
    end = int(height * y1)
    changed = 0
    total = 0
    for y in range(start, end):
        line = raw[y * row : (y + 1) * row]
        for i in range(0, len(line), 3):
            total += 1
            if abs(line[i] - 128) > 12 or abs(line[i + 1] - 128) > 12 or abs(line[i + 2] - 128) > 12:
                changed += 1
    return changed / total if total else 0.0


def _mean_volume(path: Path) -> float:
    proc = subprocess.run(
        ["ffmpeg", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True,
        text=True,
    )
    for line in proc.stderr.splitlines():
        if "mean_volume:" in line:
            return float(line.split("mean_volume:")[1].split("dB")[0].strip())
    raise AssertionError(proc.stderr[-400:])


if __name__ == "__main__":
    unittest.main()
