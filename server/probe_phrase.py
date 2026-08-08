"""譜面のフレーズ（休符で区切った塊）に、歌詞がどう割り当てられたかを出す。

user 指摘（2026-08-08）「他の選手でも、ちゃんと文で切ってる？ 中途半端な文に
なってる応援歌もあるように見える」。旋律を休符で区切ったフレーズに歌詞を
当てているので、区切りが語の途中に落ちると中途半端になる。**全曲を並べて
目で確かめられるようにする**。
"""
import asyncio
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, "/home/yasu/stackchan-server")
os.chdir("/home/yasu/stackchan-server")

import aiohttp

import cheer_song
import server_tools
import sheet_song


async def main():
    async with aiohttp.ClientSession() as s:
        songs = await server_tools._songs(s)
    for name in sorted(songs):
        if not cheer_song.sheet_source(name):
            continue
        try:
            notes, tempo, nm, _call = await asyncio.to_thread(
                lambda: None) or (None,) * 4
        except Exception:
            pass
        try:
            notes, tempo, nm, _call = sheet_song.build(
                __import__("json").load(
                    open(cheer_song.sheet_source(name), encoding="utf-8")),
                songs[name], cheer_song.moras, name)
        except Exception as e:
            print("== %s 組み立てられない: %s" % (name, e))
            continue
        # 休符でフレーズに割って、そこに乗った歌詞を並べる
        parts, cur = [], []
        for pitch, _len, lyric in notes:
            if pitch is None:
                if cur:
                    parts.append("".join(cur))
                    cur = []
                continue
            cur.append(str(lyric or ""))
        if cur:
            parts.append("".join(cur))
        print("== %s（音符 %d / 歌詞 %d モーラ）" % (name, len(notes), nm))
        for i, p in enumerate(parts, 1):
            print("   %d: %s" % (i, p))


if __name__ == "__main__":
    asyncio.run(main())
