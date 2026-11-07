"""Localhost page for the assembler.

The server binds 127.0.0.1 only. Uploads stay in work/jobs/<id>/ and are
passed to ffmpeg as file paths, never through a shell. Nothing is posted
to an external host.
"""

from __future__ import annotations

import html
import json
import re
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .pipeline import ROOT, PipelineError, build_video
from .script import build_lines, script_text
from .tts import piper_ready

JOBS = ROOT / "work" / "jobs"
MAX_REQUEST_BYTES = 420 * 1024 * 1024
MAX_FILE_BYTES = 200 * 1024 * 1024
JOB_ID = re.compile(r"^[a-f0-9]{32}$")
BUILD_LOCK = threading.Lock()

TEXT_FIELDS = {"body", "thing", "other", "color", "fontsize", "duration", "tts"}
FLAG_FIELDS = {"synthetic_video", "synthetic_bgm"}
FILE_FIELDS = {"video", "bgm", "narration"}


class LocalOnlyServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def serve(port: int = 8765) -> None:
    if port < 1 or port > 65535:
        raise PipelineError("port is out of range")
    JOBS.mkdir(parents=True, exist_ok=True)
    server = LocalOnlyServer(("127.0.0.1", port), App)
    print(f"Local assembler at http://127.0.0.1:{port}/")
    print("Bound to loopback only. No account, no upload to a hosted service.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()


class App(BaseHTTPRequestHandler):
    server_version = "RubbishLocal"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        # Method and path only. Do not log form bodies or file names from the client.
        print("[web] %s %s" % (self.address_string(), self.path.split("?", 1)[0]))

    def do_GET(self) -> None:
        if not _host_ok(self.headers.get("Host")):
            self._text(400, "bad host")
            return
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._html(200, home_page())
            return
        if parsed.path == "/preview":
            self._preview(parsed.query)
            return
        job, name = _job_route(parsed.path)
        if job and name is None:
            self._job_page(job)
            return
        if job and name:
            self._job_file(job, name)
            return
        self._text(404, "not found")

    def do_POST(self) -> None:
        if not _host_ok(self.headers.get("Host")):
            self._text(400, "bad host")
            return
        if urlparse(self.path).path != "/generate":
            self._text(404, "not found")
            return
        length = self.headers.get("Content-Length")
        if length is None or not length.isdigit():
            self._text(411, "Content-Length required")
            return
        total = int(length)
        if total > MAX_REQUEST_BYTES:
            self._text(413, "upload is larger than 420 MB")
            return
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype or "boundary=" not in ctype:
            self._text(400, "expected a multipart form")
            return
        boundary = ctype.split("boundary=", 1)[1].strip().strip('"')
        if not boundary or len(boundary) > 200:
            self._text(400, "bad multipart boundary")
            return
        if not BUILD_LOCK.acquire(blocking=False):
            self._text(429, "a video is already rendering; wait for it to finish")
            return
        job = JOBS / _new_id()
        try:
            job.mkdir(parents=True, exist_ok=False)
            os_chmod(job)
            fields, files = parse_multipart(
                _Limited(self.rfile, total),
                boundary.encode("ascii", "strict"),
                job,
            )
            result = _render(job, fields, files)
        except (PipelineError, ValueError, UnicodeError) as exc:
            shutil.rmtree(job, ignore_errors=True)
            self._html(400, error_page(str(exc)))
            return
        except Exception as exc:
            shutil.rmtree(job, ignore_errors=True)
            self._html(500, error_page(f"render failed: {exc}"))
            return
        finally:
            BUILD_LOCK.release()
        self.send_response(303)
        self.send_header("Location", f"/jobs/{job.name}")
        self._common_headers(0)
        self.end_headers()
        _ = result

    def _preview(self, query: str) -> None:
        params = parse_qs(query, keep_blank_values=True)
        def one(key: str) -> str:
            return (params.get(key) or [""])[0][:PHRASE_GUARD]

        try:
            duration = float(one("duration") or "50")
            lines = build_lines(one("body"), one("thing"), one("other"), duration)
        except (ValueError, TypeError) as exc:
            self._json(400, {"error": str(exc)})
            return
        self._json(200, {"text": script_text(lines), "lines": [line.__dict__ for line in lines]})

    def _job_page(self, job: Path) -> None:
        meta_path = job / "result.json"
        script_path = job / "script.txt"
        if not meta_path.is_file() or not (job / "final.mp4").is_file():
            self._text(404, "job not found")
            return
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        script = script_path.read_text(encoding="utf-8") if script_path.is_file() else ""
        self._html(200, result_page(job.name, meta, script))

    def _job_file(self, job: Path, name: str) -> None:
        path = (job / name).resolve()
        if path.parent != job.resolve() or not path.is_file():
            self._text(404, "not found")
            return
        kind = {
            "final.mp4": "video/mp4",
            "script.txt": "text/plain; charset=utf-8",
            "result.json": "application/json; charset=utf-8",
        }[name]
        data = path.read_bytes()
        self._bytes(200, data, kind, download=name == "script.txt")

    def _html(self, status: int, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self._common_headers(len(encoded))
        self.end_headers()
        self.wfile.write(encoded)

    def _text(self, status: int, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self._common_headers(len(encoded))
        self.end_headers()
        self.wfile.write(encoded)

    def _json(self, status: int, payload: dict) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._common_headers(len(encoded))
        self.end_headers()
        self.wfile.write(encoded)

    def _bytes(self, status: int, data: bytes, content_type: str, download: bool = False) -> None:
        start, end = 0, len(data) - 1
        status_out = status
        ranged = False
        header = self.headers.get("Range")
        if header and header.startswith("bytes=") and len(data) > 0:
            spec = header.split("=", 1)[1].split(",")[0].strip()
            if "-" in spec:
                left, right = spec.split("-", 1)
                try:
                    if left == "":
                        count = int(right)
                        start = max(0, len(data) - count)
                    else:
                        start = int(left)
                        end = int(right) if right else len(data) - 1
                    if 0 <= start <= end < len(data):
                        ranged = True
                        status_out = 206
                except ValueError:
                    ranged = False
        chunk = data[start : end + 1] if ranged else data
        self.send_response(status_out)
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        if ranged:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
        if download:
            self.send_header("Content-Disposition", "attachment")
        self._common_headers(len(chunk))
        self.end_headers()
        self.wfile.write(chunk)

    def _common_headers(self, length: int) -> None:
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; media-src 'self'; img-src 'self'; "
            "style-src 'unsafe-inline'; script-src 'unsafe-inline'",
        )


PHRASE_GUARD = 120


def _host_ok(value: str | None) -> bool:
    if not value:
        return False
    host = value.split(":")[0].strip("[]").lower()
    return host in {"127.0.0.1", "localhost"}


def _new_id() -> str:
    import uuid

    _prune()
    return uuid.uuid4().hex


def _prune(keep: int = 12) -> None:
    if not JOBS.is_dir():
        return
    jobs = [path for path in JOBS.iterdir() if path.is_dir() and JOB_ID.fullmatch(path.name)]
    jobs.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    for old in jobs[keep:]:
        if old.is_symlink():
            continue
        shutil.rmtree(old, ignore_errors=True)


def _job_route(path: str) -> tuple[Path | None, str | None]:
    parts = [part for part in path.split("/") if part]
    if len(parts) == 2 and parts[0] == "jobs" and JOB_ID.fullmatch(parts[1]):
        folder = (JOBS / parts[1]).resolve()
        if folder.is_dir() and folder.parent == JOBS.resolve():
            return folder, None
    if len(parts) == 3 and parts[0] == "jobs" and JOB_ID.fullmatch(parts[1]):
        if parts[2] not in {"final.mp4", "script.txt", "result.json"}:
            return None, None
        folder = (JOBS / parts[1]).resolve()
        if folder.is_dir() and folder.parent == JOBS.resolve():
            return folder, parts[2]
    return None, None


def os_chmod(path: Path) -> None:
    import os

    os.chmod(path, 0o700)


def _render(job: Path, fields: dict[str, str], files: dict[str, Path]):
    body = fields.get("body", "")
    thing = fields.get("thing", "")
    other = fields.get("other", "")
    color = fields.get("color") or "black"
    try:
        fontsize = int(fields.get("fontsize") or "35")
        duration = float(fields.get("duration") or "50")
    except ValueError as exc:
        raise PipelineError("font size and duration must be numbers") from exc
    tts = fields.get("tts") or "auto"
    synthetic_video = "synthetic_video" in fields
    synthetic_bgm = "synthetic_bgm" in fields
    video = files.get("video")
    bgm = files.get("bgm")
    narration = files.get("narration")
    if video is None and not synthetic_video:
        raise PipelineError("choose a video file or the synthetic test pattern")
    if bgm is None and not synthetic_bgm:
        raise PipelineError("choose background audio or the generated tone")
    return build_video(
        output=job / "final.mp4",
        video=video,
        bgm=bgm,
        body=body,
        thing=thing,
        other=other,
        color=color,
        fontsize=fontsize,
        duration=duration,
        tts=tts,
        narration=narration,
        synthetic_video=video is None and synthetic_video,
        synthetic_bgm=bgm is None and synthetic_bgm,
        pattern="testsrc",
        workdir=job,
        keep_temp=False,
        timeout=600,
    )


class _Limited:
    def __init__(self, raw, remaining: int) -> None:
        self.raw = raw
        self.remaining = remaining

    def read(self, n: int = -1) -> bytes:
        if self.remaining <= 0:
            return b""
        if n is None or n < 0 or n > self.remaining:
            n = self.remaining
        chunk = self.raw.read(n)
        self.remaining -= len(chunk)
        return chunk


class _Pushback:
    def __init__(self, raw: _Limited, limit: int) -> None:
        self.raw = raw
        self.limit = limit
        self.buf = bytearray()
        self.seen = 0

    def _pull(self) -> bool:
        chunk = self.raw.read(65536)
        if not chunk:
            return False
        self.seen += len(chunk)
        if self.seen > self.limit:
            raise PipelineError("upload exceeds the size limit")
        self.buf += chunk
        return True

    def read_until(self, marker: bytes, sink=None) -> bytes | None:
        """Return bytes before marker, or None at EOF. Optionally stream to sink."""
        emitted = bytearray()
        keep = len(marker) - 1
        while True:
            idx = self.buf.find(marker)
            if idx != -1:
                data = self.buf[:idx]
                del self.buf[: idx + len(marker)]
                if sink is not None:
                    sink.write(data)
                    return b""
                emitted += data
                return bytes(emitted)
            if len(self.buf) > keep:
                data = self.buf[:-keep] if keep else bytes(self.buf)
                del self.buf[: len(data)]
                if sink is not None:
                    sink.write(data)
                else:
                    emitted += data
            if not self._pull():
                if sink is not None:
                    sink.write(self.buf)
                    self.buf.clear()
                    return None
                emitted += self.buf
                self.buf.clear()
                return None


def parse_multipart(stream: _Limited, boundary: bytes, dest: Path) -> tuple[dict[str, str], dict[str, Path]]:
    if not boundary or any(ch in boundary for ch in b"\r\n"):
        raise PipelineError("bad boundary")
    reader = _Pushback(stream, MAX_REQUEST_BYTES)
    opener = b"--" + boundary
    preamble = reader.read_until(opener)
    if preamble is None:
        raise PipelineError("multipart body is missing its boundary")
    fields: dict[str, str] = {}
    files: dict[str, Path] = {}
    while True:
        nxt = _peek_line_kind(reader)
        if nxt == "end":
            break
        if nxt != "next":
            raise PipelineError("malformed multipart body")
        header_blob = reader.read_until(b"\r\n\r\n")
        if header_blob is None:
            raise PipelineError("truncated multipart headers")
        name, filename = _disposition(header_blob.decode("utf-8", "replace"))
        if name in FILE_FIELDS and filename is not None:
            target = dest / f"upload-{name}"
            with target.open("wb") as handle:
                found = reader.read_until(b"\r\n--" + boundary, sink=handle)
            if found is None:
                raise PipelineError("truncated upload")
            if target.stat().st_size > MAX_FILE_BYTES:
                target.unlink(missing_ok=True)
                raise PipelineError("one file is larger than 200 MB")
            if target.stat().st_size == 0:
                target.unlink()
            else:
                files[name] = target
        else:
            raw = reader.read_until(b"\r\n--" + boundary)
            if raw is None:
                raise PipelineError("truncated form field")
            if len(raw) > 4096:
                raise PipelineError("a text field is too long")
            if name in TEXT_FIELDS or name in FLAG_FIELDS:
                fields[name] = raw.decode("utf-8")
            elif name not in FILE_FIELDS:
                raise PipelineError("unexpected form field")
    return fields, files


def _peek_line_kind(reader: _Pushback) -> str:
    while len(reader.buf) < 2:
        if not reader._pull():
            break
    if reader.buf.startswith(b"--"):
        return "end"
    if reader.buf.startswith(b"\r\n"):
        del reader.buf[:2]
        return "next"
    return "bad"


def _disposition(header_blob: str) -> tuple[str, str | None]:
    name = None
    filename = None
    for line in header_blob.split("\r\n"):
        if line.lower().startswith("content-disposition:"):
            match = re.search(r'name="([^"]+)"', line)
            if match:
                name = match.group(1)
            file_match = re.search(r'filename="([^"]*)"', line)
            if file_match:
                filename = file_match.group(1)
    if not name or not re.fullmatch(r"[a-z_]{1,32}", name):
        raise PipelineError("form field name was rejected")
    return name, filename


def home_page() -> str:
    voice = (
        "Piper huayan is installed locally and will be used unless you pick espeak-ng."
        if piper_ready()
        else "Piper voice is not installed. Speech will use robotic espeak-ng until you run python3 run.py --download-voice."
    )
    return f"""<!DOCTYPE html>
<html lang="zh-Hans">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>营销号视频组装器 · local</title>
<style>
:root {{ color-scheme: light; }}
body {{ margin: 0; background: #f3efe6; color: #1d1a16; font: 16px/1.5 "WenQuanYi Micro Hei", "Noto Sans CJK SC", sans-serif; }}
main {{ max-width: 760px; margin: 0 auto; padding: 28px 18px 64px; }}
h1 {{ font-size: 1.55rem; margin: 0 0 6px; }}
h2 {{ font-size: 1.05rem; margin: 22px 0 8px; }}
p {{ margin: 8px 0; }}
.card {{ background: #fffdf8; border: 1px solid #e0d5c4; border-radius: 14px; padding: 16px 18px; margin-top: 14px; }}
.gap {{ background: #fff6e4; border-color: #ead7a6; }}
label {{ display: block; font-weight: 700; margin: 14px 0 4px; }}
.hint {{ font-weight: 400; color: #5e564c; }}
input[type=text], input[type=number], select {{ width: 100%; box-sizing: border-box; padding: 8px 10px; border-radius: 8px; border: 1px solid #cbbba6; background: #fff; }}
.check {{ font-weight: 500; }}
button {{ margin-top: 16px; background: #1d1a16; color: white; border: 0; border-radius: 10px; padding: 10px 16px; font-size: 1rem; cursor: pointer; }}
pre {{ white-space: pre-wrap; background: #211e1a; color: #f6f1e7; padding: 12px; border-radius: 10px; min-height: 8em; }}
ul {{ margin: 8px 0 0; padding-left: 1.2em; }}
</style>
</head>
<body>
<main>
<h1>营销号视频组装器</h1>
<p>Local template assembler. It writes the eight-line script, burns subtitles, and speaks them on your clip. It does <strong>not</strong> invent footage from text.</p>
<div class="card gap">
<p>{html.escape(voice)}</p>
<ul>
<li>Upstream iFlytek 小燕 is not called. A line that used to hold APPID;APIKey;APISecret is ignored.</li>
<li>Voice quality is not the same as that service. espeak-ng is robotic. Piper huayan is a different offline voice.</li>
<li>Files stay on this machine. The page is served only on 127.0.0.1.</li>
<li>There is no posting, no virality score, and no photorealistic text-to-video.</li>
</ul>
</div>
<form class="card" action="/generate" method="post" enctype="multipart/form-data">
<label>主体 subject <span class="hint">line filler, max 80 characters</span></label>
<input type="text" name="body" id="body" maxlength="80" required placeholder="核桃核">
<label>事件 event</label>
<input type="text" name="thing" id="thing" maxlength="80" required placeholder="不能吞下去">
<label>另一种说法 other wording</label>
<input type="text" name="other" id="other" maxlength="80" required placeholder="核桃核太大了，吞下去容易噎着">
<label>字幕颜色 color</label>
<select name="color">
<option value="white">white</option>
<option value="yellow">yellow</option>
<option value="black" selected>black</option>
<option value="red">red</option>
</select>
<label>字号 font size <span class="hint">pixels at the video resolution, 12–96. Upstream default 35. Use a larger number on 1080p.</span></label>
<input type="number" name="fontsize" min="12" max="96" value="35">
<label>时长 duration seconds <span class="hint">50 matches the original slots. Shorter values squash the speech.</span></label>
<input type="number" name="duration" id="duration" min="8" max="180" value="50">
<label>voice</label>
<select name="tts">
<option value="auto">auto (Piper if the model is on disk, otherwise espeak-ng)</option>
<option value="piper">piper only</option>
<option value="espeak">espeak-ng only</option>
</select>
<label>视频 video <span class="hint">optional if you tick the test pattern</span></label>
<input type="file" name="video" accept="video/*,.mp4,.mov,.mkv,.webm">
<label class="check"><input type="checkbox" name="synthetic_video" value="on"> If no video file is chosen, use a generated test pattern</label>
<label>背景声 background audio</label>
<input type="file" name="bgm" accept="audio/*,.mp3,.wav,.m4a,.aac,.ogg">
<label class="check"><input type="checkbox" name="synthetic_bgm" value="on" checked> If no audio file is chosen, use a generated tone</label>
<label>旁白 narration wav/mp3 <span class="hint">optional. Replaces TTS and is not locked to each subtitle line.</span></label>
<input type="file" name="narration" accept="audio/*,.wav,.mp3,.m4a">
<button type="submit">Assemble locally</button>
</form>
<h2>Script preview</h2>
<pre id="preview">Fill in the three lines.</pre>
</main>
<script>
async function refreshPreview() {{
  const q = new URLSearchParams({{
    body: document.getElementById('body').value,
    thing: document.getElementById('thing').value,
    other: document.getElementById('other').value,
    duration: document.getElementById('duration').value || '50'
  }});
  const box = document.getElementById('preview');
  if (!q.get('body') || !q.get('thing') || !q.get('other')) {{
    box.textContent = 'Fill in the three lines.';
    return;
  }}
  const response = await fetch('/preview?' + q.toString());
  const data = await response.json();
  box.textContent = data.text || data.error || '';
}}
for (const id of ['body', 'thing', 'other', 'duration']) {{
  document.getElementById(id).addEventListener('input', refreshPreview);
}}
</script>
</body>
</html>"""


def result_page(job_id: str, meta: dict, script: str) -> str:
    warnings = "".join(f"<li>{html.escape(str(item))}</li>" for item in meta.get("warnings") or [])
    safe_id = html.escape(job_id)
    return f"""<!DOCTYPE html>
<html lang="zh-Hans">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>assembled · local</title>
<style>
body {{ margin: 0; background: #f3efe6; color: #1d1a16; font: 16px/1.5 "WenQuanYi Micro Hei", sans-serif; }}
main {{ max-width: 760px; margin: 0 auto; padding: 28px 18px 64px; }}
video {{ width: 100%; background: #111; border-radius: 12px; }}
pre {{ white-space: pre-wrap; background: #211e1a; color: #f6f1e7; padding: 12px; border-radius: 10px; }}
.card {{ background: #fffdf8; border: 1px solid #e0d5c4; border-radius: 14px; padding: 16px 18px; margin-top: 14px; }}
a {{ color: #1d1a16; }}
</style>
</head>
<body>
<main>
<p><a href="/">Back</a></p>
<h1>Assembled</h1>
<div class="card">
<video controls src="/jobs/{safe_id}/final.mp4"></video>
<p><a href="/jobs/{safe_id}/final.mp4">Download MP4</a>
· <a href="/jobs/{safe_id}/script.txt">script.txt</a></p>
<p>Voice: {html.escape(str(meta.get("engine")))}<br>
Size: {html.escape(str(meta.get("width")))}×{html.escape(str(meta.get("height")))},
{html.escape(str(meta.get("duration")))}s,
render {html.escape(str(meta.get("elapsed_sec")))}s,
{html.escape(str(meta.get("output_bytes")))} bytes.</p>
<ul>{warnings}</ul>
</div>
<h2>Spoken script</h2>
<pre>{html.escape(script)}</pre>
</main>
</body>
</html>"""


def error_page(message: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>could not assemble</title></head>
<body style="font-family:sans-serif;max-width:720px;margin:40px auto">
<h1>Could not assemble</h1>
<p>{html.escape(message)}</p>
<p><a href="/">Back</a></p>
</body></html>"""
