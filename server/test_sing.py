"""歌わせた音が、書いた楽譜どおりの旋律になっているかを測る。

耳で確かめられないので、合成した音から基本周波数を取り出して半音に直す。
音の変わり目はしゃくり上げるので、**同じ高さが 40ms 以上続いた所**だけを 1 音
とみなす。時間で音符に割り当てる方式は取らない（合成器が前後に無音を足し、
子音の所は声にならないのでずれる。実測で確かめた）。

**ぴったり一致は求めない**。Sinsy は統計モデルの歌声で、実測すると音の高さが
±1 半音ほどゆらぐ（かつ自己相関の測り方にも誤差がある）。ここで見るのは
「旋律の形が楽譜と同じか」＝順番を保ったまま対応づけたときの平均のずれ。

  ./.venv/bin/python test_sing.py
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sing

HOP = 0.01
WIN = 0.06
MIN_RUN = 4          # 40ms 以上続いた高さだけを音とみなす
RMS_GATE = 0.004     # これ以下は無音とみなす（実測: 合成音の中央値は 0.011）
AC_GATE = 0.5        # 自己相関の山がこれ以下なら声と認めない
ok = fail = 0


def check(name, cond, got=""):
    global ok, fail
    if cond:
        ok += 1
        print("OK %s %s" % (name, got))
    else:
        fail += 1
        print("NG %s %s" % (name, got))


def semitone_track(pcm, rate):
    """10ms ごとの高さ（半音の番号）。声でない所は 0。"""
    x = np.frombuffer(pcm, dtype="<i2").astype("float64") / 32768.0
    n_win, n_hop = int(rate * WIN), int(rate * HOP)
    lo_lag, hi_lag = int(rate / 700.0), int(rate / 80.0)
    out = []
    for s in range(0, len(x) - n_win, n_hop):
        seg = x[s:s + n_win]
        if float(np.sqrt(np.mean(seg * seg))) < RMS_GATE:
            out.append(0)
            continue
        seg = seg - seg.mean()
        ac = np.correlate(seg, seg, mode="full")[len(seg) - 1:]
        if ac[0] <= 0:
            out.append(0)
            continue
        ac = ac / ac[0]
        lag = int(np.argmax(ac[lo_lag:hi_lag])) + lo_lag
        if ac[lag] <= AC_GATE:
            out.append(0)
            continue
        out.append(int(round(69 + 12 * np.log2((rate / lag) / 440.0))))
    return np.array(out)


def plateaus(track):
    """同じ高さが続いた所を 1 音にまとめる。"""
    out, i = [], 0
    while i < len(track):
        j = i
        while j < len(track) and track[j] == track[i]:
            j += 1
        if track[i] and (j - i) >= MIN_RUN:
            if not out or out[-1] != int(track[i]):
                out.append(int(track[i]))
        i = j
    return out


def score_semitones(notes):
    step = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
    out = []
    for name, _length, _lyric in notes:
        if name is None:
            continue
        s, alter, octave = sing.parse_pitch(name)
        v = 12 * (octave + 1) + step[s] + alter
        if not out or out[-1] != v:
            out.append(v)
    return out


def align_cost(a, b):
    """順番を保ったまま対応づけたときの、1 音あたりの平均のずれ（半音）。"""
    inf = float("inf")
    d = [[inf] * (len(b) + 1) for _ in range(len(a) + 1)]
    d[0][0] = 0.0
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            best = min(d[i - 1][j - 1], d[i - 1][j], d[i][j - 1])
            d[i][j] = best + abs(a[i - 1] - b[j - 1])
    return d[len(a)][len(b)] / max(len(a), len(b))


# 小節をまたぐ長い音（つなぎが要る）。Sinsy は <tie type=...> を受け付けず、
# ここで落ちていた
print("小節をまたぐ長い音…")
# 8 個目から 11 個ぶん伸ばすと小節（16 個）をまたぐ＝つなぎが要る
long_note = [["ソ4", 8, "あ"], ["ラ4", 11, "い"], ["ソ4", 21, "う"]]
try:
    pcm, rate = sing.sing_sync(long_note, tempo=120, title="長い音")
    check("小節をまたぐ音でも歌える", len(pcm) > rate,
          "%.1f 秒" % (len(pcm) / 2 / rate))
except Exception as e:
    check("小節をまたぐ音でも歌える", False, str(e)[:80])

print("歌わせる…")
pcm, rate = sing.sing_sync(sing.FURUSATO, tempo=104, title="ふるさと")
check("音が出ている", len(pcm) > rate, "%.2f 秒" % (len(pcm) / 2 / rate))

track = semitone_track(pcm, rate)
voiced = track > 0
check("声になっている所がある", voiced.sum() > 100,
      "%d/%d フレーム" % (voiced.sum(), len(track)))

got = plateaus(track)
want = score_semitones(sing.FURUSATO)
check("音の数が楽譜と釣り合っている", len(want) <= len(got) <= 2 * len(want) + 2,
      "%d 音（楽譜は %d 音）" % (len(got), len(want)))

cost = align_cost(got, want)
check("旋律の形が楽譜と同じ（平均のずれ 1.5 半音以内）", cost <= 1.5,
      "平均 %.2f 半音\n     出た音 %s\n     楽譜   %s" % (cost, got, want))

pitched = track[voiced]
check("高さの幅が 1 オクターブ半以内",
      (pitched.max() - pitched.min()) <= 18,
      "%d〜%d（半音の番号）" % (pitched.min(), pitched.max()))

print("\n%d/%d" % (ok, ok + fail))
sys.exit(1 if fail else 0)
