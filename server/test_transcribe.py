"""音源から起こした音符で歌わせて、元の旋律に戻っているかを測る。

採譜（transcribe.py）→ 歌唱（sing.py）→ もう一度その音の高さを追う、と回して、
元の音源の高さの並びと突き合わせる。順番を保った対応づけの平均のずれで見る。

  SONG=/tmp/song.mp3 ./.venv/bin/python test_transcribe.py
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sing
import transcribe

SONG = os.environ.get("SONG", "/tmp/song.mp3")
ok = fail = 0


def check(name, cond, got=""):
    global ok, fail
    if cond:
        ok += 1
        print("OK %s %s" % (name, got))
    else:
        fail += 1
        print("NG %s %s" % (name, got))


def contour(x, rate):
    """音の高さの並び（同じ高さが続く所を 1 音に畳む）。"""
    semi = transcribe.smooth(transcribe.fix_octaves(
        transcribe.f0_track(x, rate)))
    out = []
    for pitch, _a, _b in transcribe.merge_same(transcribe.segment(semi)):
        v = int(round(pitch))
        if not out or out[-1] != v:
            out.append(v)
    return out


def align_cost(a, b):
    inf = float("inf")
    d = [[inf] * (len(b) + 1) for _ in range(len(a) + 1)]
    d[0][0] = 0.0
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            d[i][j] = min(d[i - 1][j - 1], d[i - 1][j],
                          d[i][j - 1]) + abs(a[i - 1] - b[j - 1])
    return d[len(a)][len(b)] / max(len(a), len(b))


print("採譜する…")
notes, tempo, err = transcribe.transcribe(SONG)
played = [n for n in notes if n[0]]
check("音符が起こせている", len(played) >= 10,
      "%d 音 / テンポ %.0f / 割り切れなさ %.2f" % (len(played), tempo, err))

orig = contour(transcribe.load_audio(SONG), transcribe.RATE)
check("元の音源から旋律が取れている", len(orig) >= 10, "%d 音" % len(orig))

print("起こした音符で歌わせる…")
lyric_notes = [[n[0], n[1], ("ら" if n[0] else "")] for n in notes]
pcm, rate = sing.sing_sync(lyric_notes, tempo=int(round(tempo)),
                           title="うつしとった旋律")
check("歌になっている", len(pcm) > rate, "%.1f 秒" % (len(pcm) / 2 / rate))

sung = contour(np.frombuffer(pcm, dtype="<i2").astype("float64") / 32768.0,
               rate)
cost = align_cost(sung, orig)
check("元の旋律に戻っている（平均のずれ 1.5 半音以内）", cost <= 1.5,
      "平均 %.2f 半音（元 %d 音 / 歌 %d 音）" % (cost, len(orig), len(sung)))
print("     元の音源 %s" % orig[:24])
print("     歌った音 %s" % sung[:24])

print("\n%d/%d" % (ok, ok + fail))
sys.exit(1 if fail else 0)
