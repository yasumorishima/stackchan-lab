"""Opus（本体と同じ 16k / 60ms）を通した後の STT 精度・速度を測る。

実経路は「本体マイク -> Opus -> サーバー」なので、生 PCM のベンチより実際に近い。
VOICEVOX で作った既知文を Opus で往復させてから各バックエンドに食わせる。

  ./.venv/bin/python bench_stt_opus.py vosk whisper
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
import opus_codec

VV = os.environ.get("VOICEVOX_URL", "http://127.0.0.1:50021")
SPEAKER = int(os.environ.get("VOICEVOX_SPEAKER", "3"))
RATE = 16000
FRAME_MS = 60

SENTENCES = [
    "こんにちは、スタックちゃんです。",
    "今日の天気を教えて。",
    "明日の予定は何時からですか。",
    "音楽をかけてください。",
    "ありがとう、また後でね。",
]


def _post(path, params, body=None):
    url = VV + path + "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode("utf-8") if body is not None else b""
    req = urllib.request.Request(url, data=data, method="POST")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def synth(text):
    q = json.loads(_post("/audio_query", {"text": text, "speaker": SPEAKER}))
    q["outputSamplingRate"] = RATE
    q["outputStereo"] = False
    with wave.open(io.BytesIO(_post("/synthesis", {"speaker": SPEAKER}, q)), "rb") as w:
        return w.readframes(w.getnframes())


def through_opus(pcm):
    enc = opus_codec.Encoder(RATE, 1, FRAME_MS)
    dec = opus_codec.Decoder(RATE, 1)
    out = b"".join(dec.decode(p) for p in enc.encode_stream(pcm))
    enc.close()
    dec.close()
    return out


def norm(s):
    return "".join(c for c in s if c not in "、。！？,.!? ")


async def main():
    backends = sys.argv[1:] or ["vosk", "whisper"]
    clips = [(s, through_opus(synth(s))) for s in SENTENCES]

    for b in backends:
        print("== %s ==" % b)
        local_stt.warmup(b)
        hit = 0
        tot_a = tot_t = 0.0
        for s, pcm in clips:
            secs = len(pcm) / 2 / RATE
            t0 = time.monotonic()
            if b == "vosk":
                # 実装と同じ streaming 経路（60ms フレーム相当で投入）
                st = local_stt.VoskStream(RATE)
                step = RATE * 2 * FRAME_MS // 1000
                for i in range(0, len(pcm), step):
                    await st.feed(pcm[i:i + step])
                got = await st.final()
            else:
                got = await local_stt.transcribe(b, pcm, RATE)
            dt = time.monotonic() - t0
            ok = norm(got) == norm(s)
            hit += ok
            tot_a += secs
            tot_t += dt
            print("  %s %5.2fs (rtf %.2f)  %s" % ("OK  " if ok else "DIFF", dt, dt / secs, got))
        print("  exact %d/%d  audio %.2fs  stt %.2fs  rtf %.2f"
              % (hit, len(clips), tot_a, tot_t, tot_t / tot_a))


asyncio.run(main())
