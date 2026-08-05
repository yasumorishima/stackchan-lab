"""DTW（歌詞合わせ）が参照の作り方で動くのか、直した物差しで測り直す。

2026-08-05 の判断「不安定＝参照の高さ・長さを変えると 55〜1520ms 動く」は
**穴のある物差し**（音源ぜんぶと比べていた）で出した値なので、採否を決める
前に取り直す。

使い方: --controlled  /  --real <曲名>
"""
import asyncio
import os
import sys

import numpy as np

sys.path.insert(0, "/home/yasu/stackchan-server")
os.chdir("/home/yasu/stackchan-server")

import align_sung  # noqa: E402
import eval_rhythm as ev  # noqa: E402
import rhythm_eval as re_  # noqa: E402
import transcribe  # noqa: E402

PITCHES = ["F4", "A4", "C5"]


def _one(path, morae, src):
    print("  %-6s %8s" % ("参照", "ずれ"))
    devs = []
    old = align_sung.REF_PITCH
    try:
        for p in PITCHES:
            align_sung.REF_PITCH = p
            notes, tempo = align_sung.build(path, morae)
            sung, _pcm, _rate = ev._render(notes, tempo)
            d, _n = re_.deviation(sung, src)
            devs.append(d)
            print("  %-6s %6.0fms" % (p, d))
    finally:
        align_sung.REF_PITCH = old
    print("  幅 %.0fms（%.0f〜%.0f）"
          % (max(devs) - min(devs), min(devs), max(devs)))
    return devs


def controlled():
    notes, morae = ev._score(False)
    _src, pcm, rate = ev._render(notes, 1500.0)
    os.makedirs(ev.TMP, exist_ok=True)
    path = os.path.join(ev.TMP, "controlled.wav")
    ev._write(pcm, rate, path)
    src = ev._as16k(pcm, rate)
    print("正解つきの曲")
    _one(path, morae, src)


async def real(want):
    import aiohttp

    import cheer_song
    import server_tools
    async with aiohttp.ClientSession() as s:
        songs = await server_tools._songs(s)
        key = await asyncio.to_thread(server_tools._find_player, songs, want)
        text = "".join(songs.get(key, [])) if key else ""
        path = cheer_song.local_audio(key) if key else None
        if not text or not path:
            print("歌詞か音源が無い: %s" % want)
            return
        print("実曲: %s" % key)
        _one(path, cheer_song.moras(text), transcribe.load_audio(path))


def main(argv):
    if "--controlled" in argv:
        controlled()
        return 0
    if "--real" in argv:
        asyncio.run(real(argv[argv.index("--real") + 1]))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
