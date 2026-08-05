"""歌詞が対応している範囲を音源の中から見つけ、そこをモーラ数に区切る。

分かったこと（2026-08-05 実測）: 音源は歌詞を 1 対 1 で覆っていない。
1 モーラの秒数が曲によって 0.22〜1.59 秒（中央値 0.38）とばらつき、
「その他の右打者」は 14 モーラの歌詞に対して 22.2 秒鳴っている。

そこで「音源ぜんぶを歌詞で覆う」のをやめ、**始まりと終わりも一緒に決める**。
両端を自由にすると短い所へ潰れる（DTW で実証: 80→2850ms）ので、
**1 モーラの長さに絶対の縛り**を置いて潰れを防ぐ。その長さ自体も
候補の中から、いちばん良く合うものを選ぶ。

良さは 4 つ:
  ①切れ目に音の立ち上がりが来ている
  ②区間の中で高さが揃っている
  ③1 音の長さが、選んだ「1 モーラの長さ」から離れていない
  ④声の出ている所を使っている（無音を音符にしない）
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import segment_dp as sd
import transcribe

HOP = 0.01
STEP = int(os.environ.get("SPAN_STEP", "2"))          # DP は 20ms 刻み
CELLS = [0.22, 0.26, 0.30, 0.34, 0.38, 0.42, 0.46, 0.52, 0.60]
W_ONSET = float(os.environ.get("SPAN_W_ONSET", "1.0"))
W_PITCH = float(os.environ.get("SPAN_W_PITCH", "0.6"))
W_DUR = float(os.environ.get("SPAN_W_DUR", "1.2"))
W_VOICE = float(os.environ.get("SPAN_W_VOICE", "1.0"))
DUR_LO, DUR_HI = 0.45, 2.2                            # 選んだ長さの何倍まで
# 頭と尻で捨ててよいのはここまで（声のある長さに対する割合）。
# 完全に自由にすると、歌詞が全体を覆っている曲でも縮めてしまう
# （正解つきの曲で 110→790ms に悪化した）
MAX_SKIP = float(os.environ.get("SPAN_MAX_SKIP", "0.25"))


def _dp(semi, fl, n_seg, mean_f):
    """[0, T) のどこからどこまでを使ってもよい形で、n_seg 個に区切る。

    返すのは (区切り位置, 総得点)。位置は semi の添字（STEP 刻みではない）。
    """
    t = len(semi)
    m = t // STEP
    if m < n_seg + 2:
        return None, -1e18
    s = np.where(np.isnan(semi), 0.0, semi)
    ok = (~np.isnan(semi)).astype(float)
    cs = np.concatenate(([0.0], np.cumsum(s)))
    cs2 = np.concatenate(([0.0], np.cumsum(s * s)))
    cn = np.concatenate(([0.0], np.cumsum(ok)))
    f = np.zeros(t)
    f[:min(t, len(fl))] = fl[:t]

    d_lo = max(1, int(mean_f * DUR_LO / STEP))
    d_hi = max(d_lo + 1, int(mean_f * DUR_HI / STEP))
    neg = -1e18
    dp = np.full((n_seg + 1, m + 1), neg)
    back = np.zeros((n_seg + 1, m + 1), dtype=np.int32)
    skip = int(m * MAX_SKIP)
    dp[0, :skip + 1] = 0.0               # 頭で捨ててよいのはここまで
    idx = np.arange(m + 1) * STEP
    for k in range(1, n_seg + 1):
        prev = dp[k - 1]
        for b in range(d_lo, m + 1):
            a0, a1 = max(0, b - d_hi), b - d_lo
            a = np.arange(a0, a1 + 1)
            pa, pb = idx[a], idx[b]
            base = prev[a]
            live = base > neg / 2
            if not live.any():
                continue
            n = cn[pb] - cn[pa]
            n_safe = np.maximum(n, 1.0)
            mu = (cs[pb] - cs[pa]) / n_safe
            var = np.maximum(0.0, (cs2[pb] - cs2[pa]) / n_safe - mu * mu)
            pitch = np.minimum(1.0, np.sqrt(var) / 3.0)
            pitch[n < 2] = 1.0
            dur = (pb - pa).astype(float)
            voiced = n / np.maximum(dur, 1.0)
            sc = (base
                  + W_ONSET * f[pa]
                  - W_PITCH * pitch
                  - W_DUR * np.abs(np.log(dur / mean_f))
                  - W_VOICE * (1.0 - voiced))
            sc[~live] = neg
            j = int(np.argmax(sc))
            dp[k, b] = sc[j]
            back[k, b] = a[j]
    tail = m - skip                      # 尻で捨ててよいのもここまで
    b = tail + int(np.argmax(dp[n_seg, tail:]))
    if dp[n_seg, b] <= neg / 2:
        return None, -1e18
    total = float(dp[n_seg, b])
    bounds = [b]
    for k in range(n_seg, 0, -1):
        b = int(back[k, b])
        bounds.append(b)
    return [v * STEP for v in reversed(bounds)], total


def build(path, morae):
    """(音符, tempo) を返す。音符は [高さ, 10ms いくつ分, 歌詞]。"""
    if not morae:
        raise RuntimeError("歌詞が無い")
    x = transcribe.load_audio(path)
    semi = transcribe.fill_gaps(transcribe.smooth(
        transcribe.fix_octaves(transcribe.f0_track(x))))
    fl = sd.flux(x, transcribe.RATE)
    best = None
    for cell in CELLS:
        mean_f = cell / HOP
        if mean_f * len(morae) > len(semi) * 1.2:
            continue                     # その長さでは曲に収まらない
        bounds, total = _dp(semi, fl, len(morae), mean_f)
        if bounds is None:
            continue
        # 長さの違う候補を比べるので、1 音あたりに直してから比べる
        score = total / len(morae)
        if best is None or score > best[0]:
            best = (score, bounds, cell)
    if best is None:
        raise RuntimeError("区切れない")
    _score, bounds, cell = best
    notes, kept = [], []
    for k, mora in enumerate(morae):
        a, b = bounds[k], bounds[k + 1]
        if b - a < 2:
            continue
        kept.append((a, b))
        m = int((b - a) * 0.2)
        core = semi[a + m:b - m] if (b - a) - 2 * m >= 2 else semi[a:b]
        core = core[~np.isnan(core)]
        if len(core) < 1:
            core = semi[a:b][~np.isnan(semi[a:b])]
        if len(core) < 1:
            notes.append([None, int(b - a), ""])
            continue
        notes.append([transcribe.to_name(float(np.median(core))),
                      int(b - a), mora])
    if not notes:
        raise RuntimeError("音符が取れない")
    # 切れ目も返す（10ms 単位の絶対位置）。物差しはここと比べる＝音源ぜんぶと
    # 比べると、歌詞が一部しか覆わないのが正しい曲で正しい答えを不正解にする
    return notes, 1500.0, kept
