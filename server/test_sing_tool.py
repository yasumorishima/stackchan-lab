"""「歌って」と言われてから歌が出来るまでを通しで確かめる。

①さくらの LLM が sing_cheer_song を選ぶか（実際に 1 往復させる）
②その道具が音符と歌詞を用意できるか
③その音符で本当に歌になるか（長さと音量を見る）

  ./.venv/bin/python test_sing_tool.py
"""
import asyncio
import os
import sys

import aiohttp

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app
import server_tools
import sing

ok = fail = 0


def check(name, cond, got=""):
    global ok, fail
    if cond:
        ok += 1
        print("OK %s %s" % (name, got))
    else:
        fail += 1
        print("NG %s %s" % (name, got))


async def main():
    async with aiohttp.ClientSession() as session:
        # ① 言い方を変えて、歌う方の道具が選ばれるか
        for said in ("宮﨑の応援歌を歌って", "桑原の応援歌うたってよ"):
            msg = await app.sakura_chat(
                session,
                [{"role": "system", "content": app.SYSTEM_PROMPT},
                 {"role": "user", "content": said}],
                server_tools.specs())
            calls = msg.get("tool_calls") or []
            names = [c["function"]["name"] for c in calls]
            check("「%s」で歌う道具を選ぶ" % said,
                  names == ["sing_cheer_song"], str(names))

        # ② 音符と歌詞が用意できるか
        ctx = {}
        said = await server_tools.call(session, "sing_cheer_song",
                                       {"player": "宮﨑"}, ctx)
        song = ctx.get("song")
        check("道具が歌を用意する", bool(song), said)
        if not song:
            return
        notes = song["notes"]
        sung = [n for n in notes if n[0]]
        check("音符に歌詞が乗っている",
              all(n[2] for n in sung) and len(sung) >= 20,
              "%d 音・最初の 6 つ %s"
              % (len(sung), [n[2] for n in sung[:6]]))

        # ③ その音符で歌えるか
        pcm, rate = await sing.sing(notes, tempo=int(round(song["tempo"])),
                                    title=song["name"])
        secs = len(pcm) / 2 / rate
        peak = max(abs(int.from_bytes(pcm[i:i + 2], "little", signed=True))
                   for i in range(0, len(pcm), 2))
        check("歌になっている", secs > 5 and peak > 5000,
              "%.1f 秒・いちばん大きい振幅 %d" % (secs, peak))


asyncio.run(main())
print("\n%d/%d" % (ok, ok + fail))
sys.exit(1 if fail else 0)
