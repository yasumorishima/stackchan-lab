"""Opus 経由の STT を CER（文字誤り率）で比較する。

音声は VOICEVOX 合成なので人の声そのものではない＝絶対値は楽観側。
バックエンド間の相対比較と速度の目安として使う。

  ./.venv/bin/python bench_stt_cer.py sherpa vosk whisper
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
    "この部屋の温度は何度くらい。",
    "十分後にタイマーをセットして。",
    "横浜駅までの行き方を知りたい。",
    "おはよう、今日もよろしくね。",
    "その話はもう少し詳しく教えて。",
    "電気を消してくれる。",
    "今の時間は何時ですか。",
]


def _post(path, params, body=None):
    url = VV + path + "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode("utf-8") if body is not None else b""
    req = urllib.request.Request(url, data=data, method="POST")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=180) as r:
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
    return "".join(c for c in s if c not in "、。！？,.!? ー")


def levenshtein(a, b):
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


async def main():
    backends = sys.argv[1:] or ["sherpa", "vosk", "whisper"]
    clips = [(s, through_opus(synth(s))) for s in SENTENCES]
    total_audio = sum(len(p) for _, p in clips) / 2 / RATE
    print("clips=%d  audio=%.1fs  (VOICEVOX speaker %d -> Opus 16k/60ms)"
          % (len(clips), total_audio, SPEAKER))

    for b in backends:
        print("== %s ==" % b)
        local_stt.warmup(b)
        errs = chars = 0
        exact = 0
        t_all = 0.0
        for s, pcm in clips:
            t0 = time.monotonic()
            if b == "vosk":
                st = local_stt.VoskStream(RATE)
                step = RATE * 2 * FRAME_MS // 1000
                for i in range(0, len(pcm), step):
                    await st.feed(pcm[i:i + step])
                got = await st.final()
            else:
                got = await local_stt.transcribe(b, pcm, RATE)
            t_all += time.monotonic() - t0
            ref, hyp = norm(s), norm(got)
            e = levenshtein(ref, hyp)
            errs += e
            chars += len(ref)
            exact += (e == 0)
            if e:
                print("   err%2d  %s   <-  %s" % (e, got, s))
        print("  CER %.1f%%  exact %d/%d  stt %.2fs / audio %.1fs (rtf %.2f)"
              % (100.0 * errs / chars, exact, len(clips), t_all, total_audio, t_all / total_audio))


asyncio.run(main())
