"""実曲のリズムを、同じ声で歌い直したものを「音源」にして測る。

正解つきの曲（自分で書いた楽譜）は、長さも高さも素直すぎて実曲の難しさを
写していない。一方で実音源は合唱＋ブラスで、こちらの独唱と音色が違う。

そこで**実曲の立ち上がりで区切った音符（eval_oracle）を同じ声で歌わせ**、
それを音源とみなして各方式に起こし直させる。**リズムと歌詞の数は実曲のまま、
音色の違いだけを消した**条件になる。ここで戻せないなら方式の側の問題。

使い方: --real <曲名>
"""
import asyncio
import os
import sys

sys.path.insert(0, "/home/yasu/stackchan-server")
os.chdir("/home/yasu/stackchan-server")

import eval_oracle as eo  # noqa: E402
import eval_rhythm as ev  # noqa: E402


async def main(want):
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
            return 1
        morae = cheer_song.moras(text)
        notes, _x = eo.oracle_notes(path, len(morae), morae)
        src, pcm, rate = ev._render(notes, 1500.0)
        os.makedirs(ev.TMP, exist_ok=True)
        out = os.path.join(ev.TMP, "semi_%s.wav" % key)
        ev._write(pcm, rate, out)
        print("%s のリズムを同じ声で歌い直した: %d 音・%.1f 秒"
              % (key, len(notes), len(pcm) / 2.0 / rate))
        # 正解の切れ目は楽譜から厳密に分かる（合成器は 9〜13ms で従う）
        import numpy as np

        import eval_render as er
        want, _total = er.want_times(notes, 1500.0)
        want = np.array(want)
        print("  %-18s %8s %10s" % ("やり方", "ずれ", "切れ目の差"))
        for name, build in ev._methods():
            n, dev, _got, err = ev._measure(name, build, out, morae, src)
            gap = "—"
            try:
                kept = build(out, morae)[2]
                got = np.array([a * ev.HOP for a, _b in kept])
                if len(got) and len(want):
                    d = [float(np.min(np.abs(want - t))) for t in got]
                    gap = "%6.0fms" % (float(np.median(d)) * 1000)
            except Exception:                        # noqa: BLE001
                pass
            print("  %-18s %8s %10s"
                  % (n, err if err else "%6.0fms" % dev, gap))
    return 0


if __name__ == "__main__":
    rest = [a for a in sys.argv[1:] if not a.startswith("--")]
    raise SystemExit(asyncio.run(main(rest[0] if rest else "宮﨑敏郎")))
