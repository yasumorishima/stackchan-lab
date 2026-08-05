"""合成器（VOICEVOX 歌唱）が、渡した楽譜どおりの時刻に鳴らしているか。

なぜ要るか: 実音源での床が 142〜680ms もあった。原因が「合唱＋ブラスと独唱の
音色差」なら採譜を直せば良くなるが、**合成器が楽譜どおりに鳴らしていない**なら
採譜を何回直しても直らない。ここを先に切り分ける。

やり方: 楽譜が言っている音の始まりの時刻と、出来た音の立ち上がりの時刻を
突き合わせる（音どうしを比べない＝音色の話が混ざらない）。

使い方: --controlled  /  --real <曲名>
"""
import asyncio
import os
import sys

import numpy as np

sys.path.insert(0, "/home/yasu/stackchan-server")
os.chdir("/home/yasu/stackchan-server")

import eval_rhythm as ev  # noqa: E402
import rhythm_eval as re_  # noqa: E402
import sing_vv  # noqa: E402

NEAR = 0.06          # これ以内に立ち上がりがあれば「そこで鳴った」


def want_times(notes, tempo):
    """楽譜が言っている、声の出る音の始まりの時刻（秒）。"""
    score = sing_vv.to_score(notes, tempo)
    t, out = 0.0, []
    for n in score["notes"]:
        if n["key"] is not None:
            out.append(t)
        t += n["frame_length"] / float(sing_vv.FRAME_RATE)
    return out, t


def check(notes, tempo, label):
    x, pcm, rate = ev._render(notes, tempo)
    want, total = want_times(notes, tempo)
    e = re_.envelope(x)
    got = re_.peaks(e) * re_.HOP
    if len(got) < 4:
        print("  %s: 立ち上がりが %d 個" % (label, len(got)))
        return
    d = [float(np.min(np.abs(got - w))) for w in want]
    d = np.array(d)
    print("  %-16s 音 %3d・楽譜 %5.1f 秒・出来た音 %5.1f 秒"
          % (label, len(want), total, len(pcm) / 2.0 / rate))
    print("    ずれ 中央 %4.0fms・上位1割 %4.0fms・%.0f%% が %dms 以内"
          % (np.median(d) * 1000, np.percentile(d, 90) * 1000,
             100.0 * (d <= NEAR).mean(), int(NEAR * 1000)))


def controlled():
    for contrast in (False, True):
        notes, _morae = ev._score(contrast)
        check(notes, 1500.0,
              "長短の差 大" if contrast else "正解つき")


async def real(want):
    import aiohttp

    import cheer_song
    import eval_oracle as eo
    import server_tools
    async with aiohttp.ClientSession() as s:
        songs = await server_tools._songs(s)
        key = await asyncio.to_thread(server_tools._find_player, songs, want)
        text = "".join(songs.get(key, [])) if key else ""
        path = cheer_song.local_audio(key) if key else None
        if not text or not path:
            print("歌詞か音源が無い: %s" % want)
            return 1
        morae = cheer_song.moras(text)
        notes, _x = eo.oracle_notes(path, len(morae), morae)
        check(notes, 1500.0, key)
    return 0


if __name__ == "__main__":
    a = sys.argv
    if "--controlled" in a:
        controlled()
        raise SystemExit(0)
    if "--real" in a:
        raise SystemExit(asyncio.run(real(a[a.index("--real") + 1])))
    print(__doc__)
    raise SystemExit(2)
