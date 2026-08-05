"""リズムの物差し（保存版・2026-08-06）。

音の勢い（立ち上がりの強さ）の波形どうしを DTW で対応づけ、いちばん良く
合う直線からの外れの中央値を ms で返す。

なぜ直線を引くか: 全体が一様に速い/遅いのは「リズムが違う」ではない。
直線が縮尺を吸うので**伸び縮みでは減点されず、揺れだけが残る**。

⚠️ 以前は書き捨てで走らせて失われた。物差しは必ずこのファイルを使う。
⚠️ 疎な立ち上がりの突き合わせは粗すぎて使えない（元音源で 8 個しか拾えない）。

使い方:
  --calibrate <音源>                    物差しの較正
  --compare <歌> <音源> [--span 秒,秒]  歌と音源のずれ

`--span` は音源の中で歌詞が覆っている範囲。歌が音源の一部しか覆わない
とき、全体と比べると**正しい答えを不正解と採点する**。
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import align_lyrics as al
import transcribe

HOP = 0.01
RATE = transcribe.RATE
WIN = 0.032
PEAK_REL = 0.10          # いちばん強い立ち上がりに対する割合


def envelope(x, rate=RATE):
    """10ms ごとの「音の勢い」（増えた分だけ足す）。0〜1 に均す。"""
    n_win, n_hop = int(rate * WIN), int(rate * HOP)
    win = np.hanning(n_win)
    out, prev = [], None
    for s in range(0, len(x) - n_win, n_hop):
        sp = np.abs(np.fft.rfft(x[s:s + n_win] * win))
        out.append(0.0 if prev is None
                   else float(np.maximum(sp - prev, 0.0).sum()))
        prev = sp
    e = np.array(out)
    if len(e) and e.max() > 0:
        e = e / e.max()
    return e


def peaks(e):
    """立ち上がりの頂点（この時刻だけを採点に使う）。"""
    if len(e) < 3:
        return np.array([], dtype=int)
    hi = e.max() * PEAK_REL
    idx = np.where((e[1:-1] >= e[:-2]) & (e[1:-1] > e[2:])
                   & (e[1:-1] >= hi))[0] + 1
    return idx


def deviation(a, b):
    """a（歌）と b（音源）の拍のずれ。中央値 ms と本数を返す。"""
    ea, eb = envelope(a), envelope(b)
    if len(ea) < 10 or len(eb) < 10:
        raise RuntimeError("短すぎて測れない")
    warp = al.dtw_map(ea[:, None], eb[:, None])
    p = peaks(ea)
    p = p[p < len(warp)]
    if len(p) < 4:
        raise RuntimeError("立ち上がりが %d 個しか無い" % len(p))
    y = warp[p].astype(float)
    x = p.astype(float)
    # いちばん良く合う直線（縮尺と頭出しを吸わせる）
    slope, intercept = np.polyfit(x, y, 1)
    res = np.abs(y - (slope * x + intercept))
    return float(np.median(res)) * HOP * 1000.0, len(p)


def stretch(x, factor):
    """一様に伸ばす/縮める（リズムは変えない）。"""
    n = int(len(x) / factor)
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)


def jitter(x, ms, seed=0, rate=RATE, step=0.5):
    """拍だけを揺らす（全体の長さは変えない）。較正用。"""
    rng = np.random.default_rng(seed)
    dur = len(x) / float(rate)
    knots = np.arange(0.0, dur + step, step)
    off = rng.uniform(-ms / 1000.0, ms / 1000.0, len(knots))
    off[0] = off[-1] = 0.0
    src = knots + off
    src = np.maximum.accumulate(src)          # 時間は戻らない
    t = np.arange(len(x)) / float(rate)
    mapped = np.interp(t, knots, src)
    return np.interp(mapped * rate, np.arange(len(x)), x)


def _load(path):
    return transcribe.load_audio(path)


def calibrate(path):
    x = _load(path)
    rows = [("同じ音", x),
            ("5% 速め", stretch(x, 1.05)),
            ("150ms 揺らし", jitter(x, 150, seed=1)),
            ("400ms 揺らし", jitter(x, 400, seed=2))]
    print("  %-14s %8s %6s" % ("対照", "ずれ", "拍数"))
    for name, y in rows:
        d, n = deviation(y, x)
        print("  %-14s %6.0fms %6d" % (name, d, n))


def compare(sung, src, span=None):
    a = _load(sung)
    b = _load(src)
    if span:
        lo, hi = span
        b = b[int(lo * RATE):int(hi * RATE)]
    d, n = deviation(a, b)
    print("  ずれ %.0fms（拍 %d 本%s）"
          % (d, n, "" if not span else "・範囲 %.2f〜%.2f 秒" % span))
    return d


def main(argv):
    if len(argv) >= 3 and argv[1] == "--calibrate":
        calibrate(argv[2])
        return 0
    if len(argv) >= 4 and argv[1] == "--compare":
        span = None
        if "--span" in argv:
            lo, hi = argv[argv.index("--span") + 1].split(",")
            span = (float(lo), float(hi))
        compare(argv[2], argv[3], span)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
