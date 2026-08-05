"""歌える曲を全部あらかじめ作り、1 曲ずつ実測する。

出来た音は cache/songs/<名前>.sung.tNsM.wav に置かれ、本番はこれを読むだけに
なる（VOICEVOX は普段止めておける）。
"""
import asyncio
import os
import sys
import time

import numpy as np

sys.path.insert(0, "/home/yasu/stackchan-server")
os.chdir("/home/yasu/stackchan-server")

import aiohttp  # noqa: E402

import cheer_song  # noqa: E402
import server_tools  # noqa: E402
import sing_vv  # noqa: E402

HOP, WIN = 0.01, 0.06
RMS_GATE, AC_GATE = 0.004, 0.5


def track(pcm, rate):
    x = np.frombuffer(pcm, dtype="<i2").astype("float64") / 32768.0
    n_win, n_hop = int(rate * WIN), int(rate * HOP)
    lo, hi = int(rate / 700.0), int(rate / 80.0)
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
        lag = int(np.argmax(ac[lo:hi])) + lo
        out.append(0 if ac[lag] <= AC_GATE
                   else int(round(69 + 12 * np.log2((rate / lag) / 440.0))))
    return np.array(out)


def plateaus(t, min_run=4):
    out, i = [], 0
    while i < len(t):
        j = i
        while j < len(t) and t[j] == t[i]:
            j += 1
        if t[i] and (j - i) >= min_run and (not out or out[-1] != int(t[i])):
            out.append(int(t[i]))
        i = j
    return out


def align(a, b):
    if not a or not b:
        return float("nan")
    inf = float("inf")
    d = [[inf] * (len(b) + 1) for _ in range(len(a) + 1)]
    d[0][0] = 0.0
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            d[i][j] = min(d[i - 1][j - 1], d[i - 1][j],
                          d[i][j - 1]) + abs(a[i - 1] - b[j - 1])
    return d[len(a)][len(b)] / max(len(a), len(b))


def longest_silence(t):
    best = cur = 0
    for v in t:
        cur = 0 if v else cur + 1
        best = max(best, cur)
    return best * HOP


async def main():
    async with aiohttp.ClientSession() as s:
        songs = await server_tools._songs(s)
        names = list(songs)
        print(f"公式に歌詞があるのは {len(names)} 件\n")
        print("  曲                 秒   声%  ずれ  最長無音  作るのに  控え")
        okn = []
        for name in names:
            t0 = time.monotonic()
            try:
                notes, tempo, key = await cheer_song.prepare(s, name)
            except LookupError as e:
                print(f"  {name:16s}  — 歌わない（{e}）")
                continue
            except Exception as e:                    # noqa: BLE001
                print(f"  {name:16s}  — 用意できず: {type(e).__name__}: {e}")
                continue
            cached = os.path.exists(sing_vv.sung_path(key))
            try:
                pcm, rate = await asyncio.to_thread(
                    sing_vv.sung, key, notes, tempo)
            except Exception as e:                    # noqa: BLE001
                print(f"  {name:16s}  — 歌えず: {str(e)[:70]}")
                continue
            took = time.monotonic() - t0
            t = track(pcm, rate)
            shift = sing_vv.octave_shift(
                [sing_vv.to_key(n[0]) for n in notes if n[0]])
            want = []
            for n in notes:
                if n[0] is None:
                    continue
                v = sing_vv.to_key(n[0]) + shift
                if not want or want[-1] != v:
                    want.append(v)
            secs = len(pcm) / 2 / rate
            voiced = 100.0 * (t > 0).mean()
            cost = align(plateaus(t), want)
            print(f"  {key:16s} {secs:5.1f} {voiced:5.1f} {cost:5.2f} "
                  f"{longest_silence(t):8.2f} {took:8.1f}s  "
                  f"{'あった' if cached else '作った'}")
            okn.append((key, secs, voiced, cost))
        print(f"\n歌えるのは {len(okn)} 曲")
        bad = [n for n, _s, v, c in okn if v < 75 or c > 2.0]
        print(f"見直しが要りそう: {bad if bad else 'なし'}")


asyncio.run(main())
