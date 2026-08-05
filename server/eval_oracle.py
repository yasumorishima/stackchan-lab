"""実音源での「これ以上は良くならない」線（床）を測る。

なぜ要るか: 正解つきの曲（同じ声どうし）では DTW が 24ms まで出るのに、
実曲では全方式が 141〜891ms だった。**採譜が悪いのか、それとも合唱＋ブラスの
音源と ずんだもんの独唱を突き合わせている物差しの側の限界なのか**が分かれない。

そこで**音源自身の立ち上がりを正解として使う**。音源のいちばん強い立ち上がり
そのもので区切って歌わせれば、リズムとしてはこれ以上は無い。その読みが床。
どの方式も、この床と比べて評価する（合成の 400ms 揺らしと比べない）。

使い方: --real [曲名 ...]
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
MIN_GAP = 12          # 音符の最短（10ms いくつ分）


def oracle_notes(path, n_morae, morae):
    """音源自身の強い立ち上がりで区切った音符（リズムの正解）。"""
    x = transcribe.load_audio(path)
    semi = transcribe.fill_gaps(transcribe.smooth(
        transcribe.fix_octaves(transcribe.f0_track(x))))
    f = sd.flux(x, transcribe.RATE)
    voiced = np.where(~np.isnan(semi))[0]
    if len(voiced) < 10:
        raise RuntimeError("声が取れない")
    lo, hi = int(voiced[0]), min(int(voiced[-1]) + 1, len(semi))
    seg = np.zeros(len(semi))
    seg[:min(len(f), len(semi))] = f[:len(semi)]
    order = np.argsort(seg[lo:hi])[::-1] + lo
    picked = []
    for i in order:
        if len(picked) >= n_morae - 1:
            break
        if all(abs(int(i) - p) >= MIN_GAP for p in picked):
            picked.append(int(i))
    edges = [lo] + sorted(picked) + [hi]
    notes = []
    for k in range(len(edges) - 1):
        a, b = edges[k], edges[k + 1]
        if b - a < 2:
            continue
        m = int((b - a) * 0.2)
        core = semi[a + m:b - m] if (b - a) - 2 * m >= 2 else semi[a:b]
        core = core[~np.isnan(core)]
        if len(core) < 1:
            core = semi[a:b][~np.isnan(semi[a:b])]
        if len(core) < 1:
            continue
        notes.append([transcribe.to_name(float(np.median(core))),
                      int(b - a), morae[k] if k < len(morae) else "ラ"])
    return notes, x


async def main(names):
    import aiohttp

    import cheer_song
    import server_tools
    async with aiohttp.ClientSession() as s:
        songs = await server_tools._songs(s)
        if not names:
            names = ["宮﨑敏郎", "牧秀悟", "関根大気", "戸柱恭孝", "佐野恵太"]
        print("  %-10s %8s %6s" % ("曲", "床", "音符"))
        for want in names:
            key = await asyncio.to_thread(server_tools._find_player,
                                          songs, want)
            text = "".join(songs.get(key, [])) if key else ""
            path = cheer_song.local_audio(key) if key else None
            if not text or not path:
                print("  %-10s  歌詞か音源が無い" % want)
                continue
            morae = cheer_song.moras(text)
            try:
                notes, src = oracle_notes(path, len(morae), morae)
                sung, _pcm, _rate = ev._render(notes, 1500.0)
                d, _n = re_.deviation(sung, src)
            except Exception as e:                    # noqa: BLE001
                print("  %-10s  測れず: %s" % (key, str(e)[:60]))
                continue
            print("  %-10s %6.0fms %6d" % (key, d, len(notes)))


if __name__ == "__main__":
    rest = [a for a in sys.argv[1:] if not a.startswith("--")]
    raise SystemExit(asyncio.run(main(rest)) or 0)
