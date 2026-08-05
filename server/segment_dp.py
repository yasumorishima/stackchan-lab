"""歌っている区間を、モーラ数ちょうどに区切る（動的計画法で一意に決める）。

参照音声との対応づけ（DTW）は**不安定**だった＝参照の高さ・長さを変えるだけで
結果が 55〜1520ms 動き、高さの一致も 7/48 まで落ちた（2026-08-05 実測）。
合成音と実録音では似た費用の経路がいくつもあり、どれが選ばれるか決まらない。

そこで参照を使わず、**音源だけを見て切れ目を決める**。歌詞のモーラ数 N は
分かっているので、「声のある区間を N 個に区切る切り方」の中で最も良いものを
選ぶ問題になる。良さは 3 つの足し算:

  ①切れ目に音の立ち上がりが来ている（歌い出しは音が急に増える）
  ②区間の中で高さが揃っている（1 音の中で高さは動かない）
  ③長さが極端でない（曲全体の平均から離れすぎない）

動的計画法なので**答えは一意**（同じ入力なら必ず同じ結果）。
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import transcribe

HOP = 0.01
W_ONSET = float(os.environ.get("SEG_W_ONSET", "1.0"))
W_PITCH = float(os.environ.get("SEG_W_PITCH", "0.6"))
W_DUR = float(os.environ.get("SEG_W_DUR", "0.4"))
DUR_LO = float(os.environ.get("SEG_DUR_LO", "0.35"))   # 平均の何倍まで短く
DUR_HI = float(os.environ.get("SEG_DUR_HI", "2.60"))   # 平均の何倍まで長く


def flux(x, rate):
    """音の勢い（スペクトルの増分）。0〜1。"""
    n_hop = int(rate * HOP)
    win_n = 1024
    win = np.hanning(win_n)
    frames = []
    for s in range(0, max(1, len(x) - win_n), n_hop):
        frames.append(np.abs(np.fft.rfft(x[s:s + win_n] * win)))
    m = np.array(frames)
    d = np.diff(m, axis=0, prepend=m[:1])
    d[d < 0] = 0.0
    f = d.sum(axis=1)
    k = 15
    f = np.maximum(f - np.convolve(f, np.ones(k) / k, mode="same"), 0.0)
    return f / (f.max() + 1e-9)


def segment(semi, f, n_seg, lo, hi):
    """[lo, hi) を n_seg 個に区切る。区切り位置の配列を返す。"""
    span = hi - lo
    mean = span / float(n_seg)
    d_lo = max(3, int(mean * DUR_LO))
    d_hi = max(d_lo + 1, int(mean * DUR_HI))

    # 区間の中で高さが揃っているか（分散）を素早く出すための下ごしらえ
    s = np.where(np.isnan(semi), 0.0, semi)[lo:hi]
    ok = (~np.isnan(semi))[lo:hi].astype(float)
    cs = np.concatenate(([0.0], np.cumsum(s)))
    cs2 = np.concatenate(([0.0], np.cumsum(s * s)))
    cn = np.concatenate(([0.0], np.cumsum(ok)))
    fl = np.zeros(span)
    fl[:min(span, max(0, len(f) - lo))] = f[lo:lo + span]

    def pitch_cost(a, b):
        n = cn[b] - cn[a]
        if n < 2:
            return 1.0
        m = (cs[b] - cs[a]) / n
        v = max(0.0, (cs2[b] - cs2[a]) / n - m * m)
        return min(1.0, np.sqrt(v) / 3.0)      # 3 半音ばらけたら最悪

    neg = -1e18
    dp = np.full((n_seg + 1, span + 1), neg)
    back = np.zeros((n_seg + 1, span + 1), dtype=np.int32)
    dp[0, 0] = 0.0
    for k in range(1, n_seg + 1):
        for b in range(1, span + 1):
            a0 = max(0, b - d_hi)
            a1 = b - d_lo
            if a1 < a0:
                continue
            best, arg = neg, -1
            for a in range(a0, a1 + 1):
                prev = dp[k - 1, a]
                if prev <= neg / 2:
                    continue
                dur = b - a
                sc = (prev
                      + W_ONSET * fl[a]
                      - W_PITCH * pitch_cost(a, b)
                      - W_DUR * abs(np.log(dur / mean)))
                if sc > best:
                    best, arg = sc, a
            dp[k, b] = best
            back[k, b] = arg
    if dp[n_seg, span] <= neg / 2:
        raise RuntimeError("区切れない")
    bounds, b = [span], span
    for k in range(n_seg, 0, -1):
        b = int(back[k, b])
        bounds.append(b)
    bounds = [lo + v for v in reversed(bounds)]
    return bounds


def build(path, morae):
    """(音符, tempo) を返す。音符は [高さ, 10ms いくつ分, 歌詞]。"""
    if not morae:
        raise RuntimeError("歌詞が無い")
    x = transcribe.load_audio(path)
    rate = transcribe.RATE
    semi = transcribe.fill_gaps(transcribe.smooth(
        transcribe.fix_octaves(transcribe.f0_track(x))))
    voiced = np.where(~np.isnan(semi))[0]
    if len(voiced) < 10:
        raise RuntimeError("声が取れない")
    lo, hi = int(voiced[0]), min(int(voiced[-1]) + 1, len(semi))
    f = flux(x, rate)
    bounds = segment(semi, f, len(morae), lo, hi)
    notes = []
    for k, mora in enumerate(morae):
        a, b = bounds[k], bounds[k + 1]
        if b - a < 2:
            continue
        seg = semi[a:b]
        m = int((b - a) * 0.2)
        core = semi[a + m:b - m] if (b - a) - 2 * m >= 2 else seg
        core = core[~np.isnan(core)]
        if len(core) < 1:
            core = seg[~np.isnan(seg)]
        if len(core) < 1:
            notes.append([None, int(b - a), ""])
            continue
        notes.append([transcribe.to_name(float(np.median(core))),
                      int(b - a), mora])
    if not notes:
        raise RuntimeError("音符が取れない")
    return notes, 1500.0
