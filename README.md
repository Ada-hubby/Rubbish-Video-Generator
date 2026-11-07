# Rubbish-Video-Generator

Local, no-account port of [wmathor/Rubbish-Video-Generator](https://github.com/wmathor/Rubbish-Video-Generator) (讲解: [Bilibili BV1Ap4y1y7o7](https://www.bilibili.com/video/BV1Ap4y1y7o7)).

The upstream project is a **营销号 template assembler**, licensed **CC BY-NC-SA 4.0**. It is not a text-to-video model. This fork keeps that job and drops the paid iFlytek voice.

## What a run actually produces

From three short phrases it fills eight fixed Chinese lines, for example `核桃核不能吞下去是怎么回事呢？`. It then:

1. Loops or trims a video you supply to the target length (50 seconds matches the original slots).
2. Mixes a background track at -20 dB.
3. Burns the lines on as subtitles.
4. Speaks the lines with a **local** voice and places each line in its original time slot.

The picture is your file, or an FFmpeg test pattern if you ask for one. Nothing in this repository invents scenes, people, or photorealistic footage from a prompt. There is no posting step and no virality score.

## Gaps versus upstream 1.1

| Upstream | This port |
| --- | --- |
| iFlytek streaming TTS, voice `xiaoyan`, credentials on `args.txt` line 4 | Line 4 is ignored and never sent. Speech is local Piper or espeak-ng. |
| moviepy 1.0 TextClip + ImageMagick + SimHei | Subtitles are burned with FFmpeg libass and a CJK font already on the machine. |
| Bundled `ffmpeg.exe` / `ffprobe.exe` | Those Windows binaries are not executed. System `ffmpeg` and `ffprobe` are required. |
| Fixed 50 second cut | Default is still 50 seconds. 8–180 seconds is allowed; other lengths scale the same eight slots, which squishes the speech. |

Voice quality is not parity with iFlytek 小燕. On the sample script, Piper huayan fit every slot at 1.0x. espeak-ng needed about 1.3x–2.3x speedup and sounds robotic.

## Requirements

Tested here: Ubuntu, CPython 3.12.3, FFmpeg 6.1.1 (libass, libx264, aac), espeak-ng 1.51, WenQuanYi Micro Hei. No GPU. No API key. No account.

Install:

```shell
sudo apt-get install ffmpeg espeak-ng fonts-wqy-microhei
python3 run.py --check
```

Optional clearer Mandarin voice (still local, still not 小燕):

```shell
pip install -r requirements-voice.txt
python3 run.py --download-voice
```

`--download-voice` fetches about 61 MB from `huggingface.co/rhasspy/piper-voices` (`zh_CN-huayan-medium`, MIT) and checks SHA-256 `9929917bf8cabb26fd528ea44d3a6699c11e87317a14765312420be230be0f3d`. The weights stay in `models/` and are not committed. Piper is only used when that file is present. Its phoneme frontend is espeak; this port refuses voice configs that would download a second phonemizer model.

Disk: the voice file is ~61 MB. A 50 second 640×360 sample was 1.6 MB. The web UI rejects a single upload over 200 MB and a request over 420 MB. The CLI rejects an input over 500 MB. Frames above 1920×1080 are scaled down. Temporary files go under `work/` (mode 0700) and the heavy intermediates are removed after a successful CLI run.

CPU only. One 50 second 640×360 Piper render took about 6 seconds of wall clock on this machine. 1080p, 4K, and long GOP inputs were not timed. Peak RAM was not measured separately; the run finished on a 16 GB class VM without an out-of-memory kill.

## Start

Original layout, without putting a key on line 4 of `args.txt`:

```shell
# in.mp4 and in.mp3 are yours. Both are looped if they are shorter than 50s.
python3 run.py --output final.mp4
```

No footage handy (this writes a test pattern, not a generated scene):

```shell
python3 run.py --synthetic --duration 50 --output final.mp4
```

Pick the phrases yourself:

```shell
python3 run.py --synthetic --body "核桃核" --thing "不能吞下去" \
  --other "核桃核太大了，吞下去容易噎着" --tts piper --output final.mp4
```

Original silent-film demo, 9:16, about 32 seconds. It does not use the Chinese template or a voice. The picture is drawn silhouettes (not a test pattern, not a photograph). The score is synthesized in the program. It is a fictional story, and it is not a promise of views:

```shell
python3 run.py --drama --output window-seat.mp4
```

Pillow is imported only for that drawing pass: `pip install pillow`.

Local page, loopback only:

```shell
python3 run.py --serve
# http://127.0.0.1:8765/
```

The server refuses any host other than `127.0.0.1`. Do not put it on a public port.

Bring your own narration wav/mp3 if you do not want TTS. It replaces the voice from t=0 and is **not** cut to the eight subtitle slots:

```shell
python3 run.py --video in.mp4 --bgm in.mp3 --narration narration.wav --output final.mp4
```

## `args.txt`

```text
核桃核
不能吞下去
核桃核太大了，吞下去容易噎着
local
black
35
```

Lines are subject, event, other wording, ignored legacy credential slot, subtitle color, font size in pixels. Colors: `black`, `white`, `yellow`, `red`, `blue`, `green`, `cyan`, `orange`, or `#RRGGBB`. Font size is 12–96. On a 1080p frame, 35 pixels is small; raise it. Each phrase is limited to 80 characters.

## Data handling

- FFmpeg and espeak are started with argument lists. User text is not a shell command.
- The page binds `127.0.0.1` and rejects a foreign `Host` header.
- Uploads are stored under `work/jobs/<id>/` with generated names. Only `final.mp4`, `script.txt`, and `result.json` are served back, and only for that id.
- One render at a time. Older jobs beyond the last 12 are deleted.
- No analytics, no transcript upload, no read of environment secrets.

`work/` can hold your footage until you delete it.

## What was run

`python3 -m unittest tests.test_pipeline tests.test_drama` — 16 tests, about 12 seconds, all passing on this machine. `test_drama` checks the silent-film script only. The rendered MP4 was checked separately with ffprobe: 32.0 s, 720×1280, SAR 1:1, DAR 9:16, 24 fps, H.264 + stereo AAC, mean volume about -25 dB.

- The eight lines and the 0/4/11/15/23/29/34/40 second slots at duration 50. Long lines insert ASS line breaks so they stay inside a 640-pixel frame.
- Slot scaling at 25 seconds.
- Rejection of control characters, and `args.txt` line 4 not copied into the parsed settings.
- Multipart parsing of a text field and a file field.
- 8 second gray synthetic clip: H.264 + audio, duration within 0.4 s, subtitle pixels in the lower band, upper band still flat gray. A subject containing `$(touch …)` did not create the touched file.
- 50 second espeak timeline on a test pattern: duration 50 s, audible (mean level above -45 dB).
- User-supplied 320×240 clip, short tone, and a narration file. Output stayed 320 wide and did not call TTS.
- Loopback HTTP server: home page, multipart POST, 303 to the job, MP4 bytes, script visible. It listens on `127.0.0.1` only.
- Source scan: the iFlytek websocket host and `CERT_NONE` are not in the Python sources.

Sample file (not committed): 50.0 s, 640×360, 24 fps, H.264 + AAC stereo 44.1 kHz, 1.6 MB, mean volume about -22 dB. Piper speech for the walnut example fit the original slots with no speed-up. The picture is FFmpeg `testsrc`. Frames at 1 s, 16 s, 35 s, and 42 s show the matching subtitle lines, including the wrapped longer lines.

## Not tested

Windows and macOS. 1080p and the 4K downscale branch. Durations other than the unit-tested 8 s, 25 s (math only), and 50 s. Non-Mandarin text. Odd codecs, damaged files, and inputs near the size cap. Concurrent renders (the server returns 429 instead). A second download of the voice after the pinned file was already fetched. Listening quality beyond "the lines are present and timed"; Piper is intelligible in the sense that the phoneme timings fit, and it is still not 小燕. espeak at 2.3× on the shortest line is hard to follow.

Runtime cost is local electricity and CPU. There is no per-render API fee because no paid API is called. Nothing here predicts views.

## Layout

`run.py` is the command. `rubbish_video/` holds the script template, TTS, ffmpeg assembly, and the loopback page. Upstream `ffmpeg.exe` and `ffprobe.exe` remain in the tree unused.

License: [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/). Credit wmathor. Non-commercial. Share derivatives the same way.
