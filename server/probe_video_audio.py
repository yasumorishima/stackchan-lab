"""動画の音声に人の声（歌以外の発話）が入っていないかを確かめる。

公開前の確認用。3 秒ずつ区切って認識器に通し、拾えた文字を並べる。
"""
import os
import sys
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("SHERPA_TRIM", "0")
import local_stt  # noqa: E402

path = sys.argv[1]
step = 3.0
with wave.open(path, "rb") as w:
    rate = w.getframerate()
    total = w.getnframes()
    raw = w.readframes(total)

print("長さ %.1f 秒 / %d Hz" % (total / float(rate), rate))
n = int(step * rate) * 2
for i in range(0, len(raw), n):
    chunk = raw[i:i + n]
    if len(chunk) < rate:
        break
    text = local_stt._sherpa_sync(chunk, rate)
    print("%5.1f-%5.1f  %s" % (i / 2.0 / rate, (i + len(chunk)) / 2.0 / rate,
                               text or "(なし)"))
