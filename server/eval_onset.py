"""実曲用のリズムの物差し。**音どうしでなく時刻どうしを比べる**。

なぜ替えるか（2026-08-06 実測）:
  - 合成器は楽譜どおりに鳴らしている（ずれ中央 9〜13ms・96〜98% が 60ms 以内）
  - なのに実音源との音どうしの比較は床が 142〜680ms あった
    （音源自身の立ち上がりで区切って歌わせても、そこまでしか下がらない）
  → 合唱＋ブラスの音源と ずんだもんの独唱では、音の勢いの形がそもそも違う。
    音どうしの物差しは実曲では使えない。

やり方: 方式が出した**音符の始まりの時刻**が、音源の**立ち上がりの時刻**に
どれだけ乗っているか（いちばん近い立ち上がりまでの距離の中央値）。

⚠️ 立ち上がりが多い曲ほど近くに何かある＝甘く出る。だから必ず対照を並べる:
  - でたらめ: 同じ本数の切れ目を乱数で置く（5 通りの中央値）
  - 等間隔  : 声のある範囲を同じ本数で等分
方式の値が対照より**はっきり小さい**ときだけ、当たっていると読む。

使い方: [曲名 ...]
"""
import asyncio
import os
import sys

import numpy as np

sys.path.insert(0, "/home/yasu/stackchan-server")
os.chdir("/home/yasu/stackchan-server")

import eval_rhythm as ev  # noqa: E402
import rhythm_eval as re_  # noqa: E402
import segment_dp as sd  # noqa: E402
import transcribe  # noqa: E402

HOP = 0.01
PEAK_REL = float(os.environ.get("ONSET_REL", "1.6"))
MEAN_WIN = float(os.environ.get("ONSET_MEAN_WIN", "0.30"))
MIN_GAP = float(os.environ.get("ONSET_MIN_GAP", "0.12"))


def onsets(x):
    """音源の立ち上がりの時刻（秒）。

    ⚠️ ここが 2 回とも落とし穴だった。
    ①`segment_dp.flux` の山では、正解つきの曲で 正解 55.0ms・でたらめ 79.5ms
      ＝1.45 倍しか離れず読めない。
    ②`rhythm_eval.envelope` の山を全部拾うと、実音源では **25 秒に 742 本**
      （34ms ごと）取れてしまい、**でたらめでも 8.9ms** に収まる＝飽和して
      どの方式も同点になる。

    そこで**まわりより目立つ山だけ**を採り、**最短間隔**を置く。曲の長さに
    対する本数が、歌詞のモーラ数と同じくらいになるのが狙い。
    """
    e = re_.envelope(x)
    if len(e) < 5:
        return np.array([])
    w = int(MEAN_WIN / re_.HOP)
    ker = np.ones(2 * w + 1) / float(2 * w + 1)
    local = np.convolve(e, ker, mode="same")
    cand = np.where((e[1:-1] >= e[:-2]) & (e[1:-1] > e[2:])
                    & (e[1:-1] >= local[1:-1] * PEAK_REL))[0] + 1
    if len(cand) == 0:
        return np.array([])
    gap = int(MIN_GAP / re_.HOP)
    out = []
    for i in cand[np.argsort(e[cand])[::-1]]:       # 強い山から採る
        if all(abs(int(i) - j) >= gap for j in out):
            out.append(int(i))
    return np.sort(np.array(out)) * re_.HOP


def score(starts, ons):
    """切れ目からいちばん近い立ち上がりまでの距離の中央値（ms）。"""
    if len(starts) < 2 or len(ons) < 2:
        return float("nan")
    d = [float(np.min(np.abs(ons - t))) for t in starts]
    return float(np.median(d)) * 1000.0


def controls(lo, hi, n, ons, seeds=5):
    span = hi - lo
    even = lo + np.arange(n) * (span / float(n))
    rng = np.random.default_rng(0)
    rand = [score(np.sort(rng.uniform(lo, hi, n)), ons)
            for _ in range(seeds)]
    return score(even, ons), float(np.median(rand))


async def main(names):
    import aiohttp

    import cheer_song
    import server_tools
    async with aiohttp.ClientSession() as s:
        songs = await server_tools._songs(s)
        if not names:
            names = ["宮﨑敏郎", "牧秀悟", "関根大気", "戸柱恭孝", "佐野恵太"]
        print("  %-10s %-18s %8s" % ("曲", "やり方", "ずれ"))
        for want in names:
            key = await asyncio.to_thread(server_tools._find_player,
                                          songs, want)
            text = "".join(songs.get(key, [])) if key else ""
            path = cheer_song.local_audio(key) if key else None
            if not text or not path:
                print("  %-10s  歌詞か音源が無い" % want)
                continue
            morae = cheer_song.moras(text)
            x = transcribe.load_audio(path)
            ons = onsets(x)
            rows, allk = [], []
            for name, build in ev._methods():
                try:
                    kept = build(path, morae)[2]
                except Exception as e:               # noqa: BLE001
                    rows.append((name, None, str(e)[:50]))
                    continue
                allk.append(kept)
                starts = np.array([a * HOP for a, _b in kept])
                rows.append((name, score(starts, ons), None))
            # 対照は、どの方式も入る広さで作る（甘い/辛いを片寄らせない）
            if allk:
                lo = min(k[0][0] for k in allk) * HOP
                hi = max(k[-1][1] for k in allk) * HOP
                n = int(np.median([len(k) for k in allk]))
                even, rand = controls(lo, hi, n, ons)
            else:
                even = rand = float("nan")
            print("  %-10s 立ち上がり %d 本・%.1f 秒"
                  % (key, len(ons), len(x) / float(transcribe.RATE)))
            for name, v, err in rows:
                print("    %-18s %s"
                      % (name, err if err else "%6.1fms" % v))
            print("    %-18s %6.1fms" % ("対照: 等間隔", even))
            print("    %-18s %6.1fms" % ("対照: でたらめ", rand))


def controlled():
    """物差しそのものの検算＝正解の切れ目が、対照よりはっきり小さいか。"""
    notes, morae = ev._score(False)
    x, pcm, rate = ev._render(notes, 1500.0)
    ons = onsets(x)
    lead = 0.5
    t, starts = lead, []
    for _p, d, _m in notes:
        starts.append(t)
        t += d * HOP
    even, rand = controls(lead, t, len(notes), ons)
    print("正解つきの曲: 立ち上がり %d 本" % len(ons))
    print("  %-18s %6.1fms" % ("正解の切れ目", score(np.array(starts), ons)))
    print("  %-18s %6.1fms" % ("対照: 等間隔", even))
    print("  %-18s %6.1fms" % ("対照: でたらめ", rand))


if __name__ == "__main__":
    if "--controlled" in sys.argv:
        controlled()
        raise SystemExit(0)
    rest = [a for a in sys.argv[1:] if not a.startswith("--")]
    raise SystemExit(asyncio.run(main(rest)) or 0)
