"""歌詞を手掛かりに、音源のどこで各モーラが歌われているかを割り出す。

**これが採譜の本体**（音の高さの変わり目で切る旧方式は、正解が分かっている
合成曲で試すと音の数も長さも高さも復元できなかった＝ずれ中央 850〜1425ms・
高さ完全一致 4/24）。

やり方:
  1. **同じ声（VOICEVOX ずんだもん）**で、歌詞を一定の高さ・一定の長さで歌わせた
     参照を作る。参照の中で各モーラが何秒目かは、こちらが決めた値そのもの
  2. 参照と音源を MFCC で対応づける（DTW）。音色が同じなので対応が付く
     （参照を Open JTalk の**話し声**にすると外れる＝ずれ 1770ms で実証済み）
  3. 参照の各モーラ開始時刻を音源側へ写す＝音符の切れ目
  4. その区間の**真ん中 60%** の高さの中央値を、その音の高さにする
     （前後は しゃくり上げ と 次の音への渡り なので混ぜない）

正解つきの合成曲での実測: ずれ中央 65ms（較正: 150ms 揺らしで 73ms）。
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import align_lyrics as al
import sing_vv
import transcribe

HOP = 0.01
REF_CELL = int(os.environ.get("ALIGN_REF_CELL", "25"))     # 参照 1 モーラ 250ms
REF_PITCH = os.environ.get("ALIGN_REF_PITCH", "A4")
CORE = 0.6                                                 # 高さを見る真ん中の割合


def _ref(morae, cells=None):
    """同じ声で、一定の高さ・長さで歌わせた参照と、各モーラの開始秒。"""
    # cells を渡すとモーラごとに長さを変えられる（合わせ直し用）
    if cells is None:
        cells = [REF_CELL] * len(morae)
    notes = [[REF_PITCH, max(4, int(c)), m]
             for c, m in zip(cells, morae)]
    pcm, rate = sing_vv.sing_sync(notes, 1500.0)
    x = np.frombuffer(pcm, dtype="<i2").astype("float64") / 32768.0
    if rate != al.RATE:
        x = np.interp(np.arange(0, len(x), rate / al.RATE),
                      np.arange(len(x)), x)
    lead = 0.5                       # sing_vv が前に置く休み
    starts, t = [], lead
    for c in cells:
        starts.append(t)
        t += max(4, int(c)) * HOP
    return x, starts


def _pitch(semi, a, b):
    """区間の真ん中 60% の高さ（半音）。声が無ければ None。"""
    n = b - a
    if n <= 0:
        return None
    m = int(n * (1.0 - CORE) / 2.0)
    seg = semi[a + m:b - m] if n - 2 * m >= 2 else semi[a:b]
    seg = seg[~np.isnan(seg)]
    if len(seg) < 1:
        seg = semi[a:b]
        seg = seg[~np.isnan(seg)]
    if len(seg) < 1:
        return None
    return float(np.median(seg))


def build(path, morae):
    """(音符, tempo) を返す。音符は [高さ, 10ms いくつ分, 歌詞]。"""
    if not morae:
        raise RuntimeError("歌詞が無い")
    x = transcribe.load_audio(path)
    semi = transcribe.fill_gaps(transcribe.smooth(
        transcribe.fix_octaves(transcribe.f0_track(x))))
    # 歌っている所だけを相手にする（前奏・後奏を歌詞へ割り当てない）
    voiced = np.where(~np.isnan(semi))[0]
    if len(voiced) < 10:
        raise RuntimeError("声が取れない")
    pad = int(0.15 / HOP)
    lo = max(0, int(voiced[0]) - pad)
    hi = min(len(semi), int(voiced[-1]) + pad)
    n_lo = int(lo * HOP * transcribe.RATE)
    n_hi = int(hi * HOP * transcribe.RATE)
    # DTW は 2 つの時間軸の縮尺が近いほど当たる。参照を 250ms 固定に
    # していたので曲によっては 2 倍近く伸ばす必要があり、そこで外れて
    # いた。1 回目は「声のある長さ ÷ モーラ数」、2 回目は 1 回目で
    # 分かったモーラごとの長さで参照を作り直す
    span = hi - lo
    cells = [max(4, int(round(span / float(len(morae)))))] * len(morae)
    target = al.mfcc(x[n_lo:n_hi])
    bounds = None
    for _pass in range(2):
        ref, starts = _ref(morae, cells)
        warp = al.dtw_map(al.mfcc(ref), target)
        bounds = [lo + int(warp[min(int(t / HOP), len(warp) - 1)])
                  for t in starts]
        edges = bounds + [hi]
        cells = [max(4, edges[k + 1] - edges[k])
                 for k in range(len(morae))]
    bounds.append(hi)
    notes = []
    for k, mora in enumerate(morae):
        a = bounds[k]
        b = min(max(bounds[k + 1], a + 2), len(semi))
        if b - a < 2:
            continue
        p = _pitch(semi, a, b)
        if p is None:
            notes.append([None, int(b - a), ""])
            continue
        notes.append([transcribe.to_name(p), int(b - a), mora])
    if not notes:
        raise RuntimeError("音符が取れない")
    return notes, 1500.0
