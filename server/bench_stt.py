"""ローカル STT の実測ベンチ。

VOICEVOX で作った既知の日本語文を 16k PCM にして vosk / faster-whisper に食わせ、
認識文・所要時間・RTF（所要 / 音声長）を出す。実機マイク音声ではないので
「認識精度の上限側」の目安だが、どちらを既定にするかの判断には足りる。
"""
import asyncio
import io
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import wave

import local_stt

VV = os.environ.get("VOICEVOX_URL", "http://127.0.0.1:50021")
SPEAKER = int(os.environ.get("VOICEVOX_SPEAKER", "3"))

SENTENCES = [
    "こんにちは、スタックちゃんです。",
    "今日の天気を教えて。",
    "明日の予定は何時からですか。",
    "音楽をかけてください。",
    "ありがとう、また後でね。",
]


def post(path, params, body=None):
    url = VV + path + "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode("utf-8") if body is not None else b""
    req = urllib.request.Request(url, data=data, method="POST")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def synth_pcm16k(text):
    q = json.loads(post("/audio_query", {"text": text, "speaker": SPEAKER}))
    q["outputSamplingRate"] = 16000
    q["outputStereo"] = False
    wav = post("/synthesis", {"speaker": SPEAKER}, q)
    with wave.open(io.BytesIO(wav), "rb") as w:
        assert w.getnchannels() == 1 and w.getsampwidth() == 2, "unexpected wav"
        assert w.getframerate() == 16000, w.getframerate()
        return w.readframes(w.getnframes())


async def main():
    backends = sys.argv[1:] or ["vosk", "whisper"]
    clips = []
    for s in SENTENCES:
        t0 = time.monotonic()
        pcm = synth_pcm16k(s)
        clips.append((s, pcm, time.monotonic() - t0))
    print("== TTS (VOICEVOX speaker %d) ==" % SPEAKER)
    for s, pcm, dt in clips:
        secs = len(pcm) / 2 / 16000
        print("  %5.2fs audio / synth %5.2fs (rtf %.2f)  %s" % (secs, dt, dt / secs, s))

    for b in backends:
        print("== %s ==" % b)
        t0 = time.monotonic()
        local_stt.warmup(b)
        print("  load %.1fs" % (time.monotonic() - t0))
        tot_a = tot_t = 0.0
        for s, pcm, _ in clips:
            secs = len(pcm) / 2 / 16000
            t1 = time.monotonic()
            got = await local_stt.transcribe(b, pcm, 16000)
            dt = time.monotonic() - t1
            tot_a += secs
            tot_t += dt
            mark = "OK " if got.replace("、", "").replace("。", "") == s.replace("、", "").replace("。", "") else "DIFF"
            print("  %s %5.2fs (rtf %.2f)  %s" % (mark, dt, dt / secs, got))
        print("  total audio %.2fs  stt %.2fs  rtf %.2f" % (tot_a, tot_t, tot_t / tot_a))


asyncio.run(main())
