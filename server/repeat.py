"""音源が同じ旋律を何度も繰り返しているかを調べる。

歌詞は 1 回ぶんしか無いのに音源が 4 回繰り返していると、どんな区切り方をしても
合わない（実測: 「その他の右打者」は 1 モーラ 1.58 秒＝ふつうの 4.16 倍）。
高さの動きの自己相関で繰り返しの周期を出し、**1 回ぶんの範囲**を返す。
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import transcribe

HOP = 0.01
MIN_PERIOD = float(os.environ.get("REPEAT_MIN_SEC", "3.0"))
PEAK = float(os.environ.get("REPEAT_PEAK", "0.55"))


def find_period(semi, lo, hi):
    """[lo, hi) の中の繰り返し周期（フレーム数）。無ければ None。"""
    seg = semi[lo:hi]
    v = np.where(np.isnan(seg), 0.0, seg)
    ok = (~np.isnan(seg)).astype(float)
    if ok.sum() < 20:
        return None, 0.0
    v = v - (v.sum() / max(ok.sum(), 1.0)) * ok      # 声のある所だけ中心化
    n = len(v)
    ac = np.correlate(v, v, mode="full")[n - 1:]
    if ac[0] <= 0:
        return None, 0.0
    # 重なりの長さで割る（ずらすほど重なりが減るため）
    counts = np.arange(n, 0, -1).astype(float)
    ac = (ac / counts) / (ac[0] / n)
    lo_lag = int(MIN_PERIOD / HOP)
    hi_lag = n // 2
    if hi_lag <= lo_lag + 2:
        return None, 0.0
    k = int(np.argmax(ac[lo_lag:hi_lag])) + lo_lag
    return (k, float(ac[k])) if ac[k] >= PEAK else (None, float(ac[k]))


def one_pass(path, n_morae):
    """(lo, hi, 繰り返し回数, 自己相関の高さ) を返す。hi は 1 回ぶんの終わり。"""
    x = transcribe.load_audio(path)
    semi = transcribe.fill_gaps(transcribe.smooth(
        transcribe.fix_octaves(transcribe.f0_track(x))))
    v = np.where(~np.isnan(semi))[0]
    if len(v) < 10:
        raise RuntimeError("声が取れない")
    lo, hi = int(v[0]), min(int(v[-1]) + 1, len(semi))
    period, score = find_period(semi, lo, hi)
    if period is None:
        return lo, hi, 1, score
    times = max(1, int(round((hi - lo) / float(period))))
    if times < 2:
        return lo, hi, 1, score
    return lo, lo + period, times, score


if __name__ == "__main__":
    import asyncio

    import aiohttp

    import cheer_song
    import server_tools

    async def main():
        async with aiohttp.ClientSession() as s:
            songs = await server_tools._songs(s)
            print("  曲              ﾓｰﾗ 全体(秒) 1回ぶん(秒) 回数 一致度 "
                  "1ﾓｰﾗ(秒)")
            for name in songs:
                path = cheer_song.local_audio(name)
                if not path:
                    continue
                morae = cheer_song.moras("".join(songs.get(name, [])))
                if not morae:
                    continue
                try:
                    lo, hi, times, score = one_pass(path, len(morae))
                except Exception as e:                # noqa: BLE001
                    print(f"  {name}: {type(e).__name__}: {str(e)[:40]}")
                    continue
                x = transcribe.load_audio(path)
                print(f"  {name:14s} {len(morae):4d} "
                      f"{len(x)/transcribe.RATE:8.1f} "
                      f"{(hi-lo)*HOP:10.1f} {times:5d} {score:6.2f} "
                      f"{(hi-lo)*HOP/len(morae):8.2f}")

    asyncio.run(main())
