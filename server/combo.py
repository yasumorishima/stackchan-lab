"""DP で骨格を決め、その長さを初期値にして DTW で仕上げる（組み合わせ）。

- DP（`segment_dp`）は答えが一意で安定だが、高さの当たりが弱い
- DTW（`align_sung`）は音色が合えば当たるが、**参照の作り方で結果が動く**
  （参照の高さ・長さを変えると 55〜1520ms ずれた）
→ DP の結果を DTW の参照の長さに使えば、任意性が消えて安定し、
  仕上げの精度は DTW のものが得られるはず。ここではそれを測る。
"""
import os
import sys

import numpy as np

sys.path.insert(0, "/home/yasu/stackchan-server")
os.chdir("/home/yasu/stackchan-server")

import align_lyrics as al  # noqa: E402
import align_sung  # noqa: E402
import segment_dp as sd  # noqa: E402
import transcribe  # noqa: E402

HOP = 0.01


def build(path, morae, ref_pitch="A4", passes=1):
    x = transcribe.load_audio(path)
    semi = transcribe.fill_gaps(transcribe.smooth(
        transcribe.fix_octaves(transcribe.f0_track(x))))
    voiced = np.where(~np.isnan(semi))[0]
    if len(voiced) < 10:
        raise RuntimeError("声が取れない")
    lo, hi = int(voiced[0]), min(int(voiced[-1]) + 1, len(semi))
    f = sd.flux(x, transcribe.RATE)
    bounds = sd.segment(semi, f, len(morae), lo, hi)      # DP の骨格
    cells = [max(4, bounds[k + 1] - bounds[k]) for k in range(len(morae))]

    n_lo = int(lo * HOP * transcribe.RATE)
    n_hi = int(hi * HOP * transcribe.RATE)
    target = al.mfcc(x[n_lo:n_hi])
    old_pitch = align_sung.REF_PITCH
    align_sung.REF_PITCH = ref_pitch
    try:
        for _p in range(passes):
            ref, starts = align_sung._ref(morae, cells)
            warp = al.dtw_map(al.mfcc(ref), target)
            bounds = [lo + int(warp[min(int(t / HOP), len(warp) - 1)])
                      for t in starts]
            edges = bounds + [hi]
            cells = [max(4, edges[k + 1] - edges[k])
                     for k in range(len(morae))]
    finally:
        align_sung.REF_PITCH = old_pitch

    edges = bounds + [hi]
    notes, kept = [], []
    for k, mora in enumerate(morae):
        a, b = edges[k], edges[k + 1]
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
    return notes, 1500.0, kept
