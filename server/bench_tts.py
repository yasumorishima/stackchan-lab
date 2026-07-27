"""VOICEVOX の合成速度を測る（audio_query + synthesis、同じ文を 3 回）。"""
import io
import json
import os
import statistics
import sys
import time
import urllib.parse
import urllib.request
import wave

VV = os.environ.get("VOICEVOX_URL", "http://127.0.0.1:50021")
SPEAKER = int(os.environ.get("VOICEVOX_SPEAKER", "3"))
TEXT = sys.argv[1] if len(sys.argv) > 1 else "今日はいい天気ですね。"


def post(path, params, body=None):
    url = VV + path + "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode("utf-8") if body is not None else b""
    req = urllib.request.Request(url, data=data, method="POST")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=180) as r:
        return r.read()


times = []
for i in range(3):
    t0 = time.monotonic()
    q = json.loads(post("/audio_query", {"text": TEXT, "speaker": SPEAKER}))
    wav = post("/synthesis", {"speaker": SPEAKER}, q)
    dt = time.monotonic() - t0
    with wave.open(io.BytesIO(wav), "rb") as w:
        secs = w.getnframes() / w.getframerate()
    times.append(dt)
    print("  run %d: %.2fs for %.2fs audio (rtf %.2f)" % (i + 1, dt, secs, dt / secs))
print("  median %.2fs" % statistics.median(times))
