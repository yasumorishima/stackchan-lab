"""DTW（歌詞合わせ）の参照の作り方を、実曲用の物差し（時刻どうし）で選ぶ。

見立て: 参照は ずんだもん（女声）で作っているが、応援歌の音源は男声の合唱＋
ブラス。MFCC で突き合わせる以上、**参照の声が音源に近いほど当たる**はず。
VOICEVOX の歌唱には男声もある（玄野武宏 3011・青山龍星 3013・剣崎雌雄 3021）。

⚠️ ここで選ぶのは**中で使う参照の声**であって、歌う声ではない（歌う声は
user 様が決めた ずんだもん のまま）。

使い方: <曲名> [曲名 ...]
"""
import asyncio
import functools
import os
import sys

import numpy as np

sys.path.insert(0, "/home/yasu/stackchan-server")
os.chdir("/home/yasu/stackchan-server")

import align_sung  # noqa: E402
import eval_onset as eo  # noqa: E402
import sing_vv  # noqa: E402
import transcribe  # noqa: E402

HOP = 0.01
# (見出し, 参照の声, 参照の高さ)
REFS = [("ずんだもん A4", 3003, "A4"),
        ("ずんだもん F4", 3003, "F4"),
        ("玄野武宏 A3", 3011, "A3"),
        ("青山龍星 A3", 3013, "A3"),
        ("青山龍星 D3", 3013, "D3"),
        ("剣崎雌雄 A3", 3021, "A3")]


async def main(names):
    import aiohttp

    import cheer_song
    import server_tools
    async with aiohttp.ClientSession() as s:
        songs = await server_tools._songs(s)
        for want in names:
            key = await asyncio.to_thread(server_tools._find_player,
                                          songs, want)
            text = "".join(songs.get(key, [])) if key else ""
            path = cheer_song.local_audio(key) if key else None
            if not text or not path:
                print("  %s: 歌詞か音源が無い" % want)
                continue
            morae = cheer_song.moras(text)
            x = transcribe.load_audio(path)
            ons = eo.onsets(x)
            print("%s（立ち上がり %d 本・モーラ %d）" % (key, len(ons), len(morae)))
            # ⚠️ `sing_sync(notes, tempo, singer=SINGER)` の既定は読み込み時に
            # 決まる＝`sing_vv.SINGER` を後から書き換えても効かない。包み直す
            old_p, old_f = align_sung.REF_PITCH, sing_vv.sing_sync
            vals = []
            try:
                for label, singer, pitch in REFS:
                    align_sung.REF_PITCH = pitch
                    sing_vv.sing_sync = functools.partial(old_f,
                                                          singer=singer)
                    try:
                        kept = align_sung.build(path, morae)[2]
                    except Exception as e:            # noqa: BLE001
                        print("  %-14s 起こせず: %s" % (label, str(e)[:50]))
                        continue
                    starts = np.array([a * HOP for a, _b in kept])
                    v = eo.score(starts, ons)
                    vals.append(v)
                    print("  %-14s %6.1fms" % (label, v))
            finally:
                align_sung.REF_PITCH = old_p
                sing_vv.sing_sync = old_f
            lo = min(k[0][0] for k in [kept]) * HOP
            hi = max(k[-1][1] for k in [kept]) * HOP
            even, rand = eo.controls(lo, hi, len(morae), ons)
            print("  %-14s %6.1fms" % ("対照: 等間隔", even))
            print("  %-14s %6.1fms" % ("対照: でたらめ", rand))
            if vals:
                print("  参照で動く幅 %.1fms（%.1f〜%.1f）"
                      % (max(vals) - min(vals), min(vals), max(vals)))
    return 0


if __name__ == "__main__":
    rest = [a for a in sys.argv[1:] if not a.startswith("--")]
    raise SystemExit(asyncio.run(main(rest or ["宮﨑敏郎"])))
