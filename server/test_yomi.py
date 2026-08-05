"""当て字で選手に当たるかを確かめる。"""
import asyncio
import os
import sys

sys.path.insert(0, "/home/yasu/stackchan-server")
os.chdir("/home/yasu/stackchan-server")

import aiohttp  # noqa: E402

import server_tools  # noqa: E402

CASES = [("真木", "牧秀悟"), ("牧", "牧秀悟"), ("宮崎", "宮﨑敏郎"),
         ("宮﨑敏郎", "宮﨑敏郎"), ("さの", "佐野恵太"), ("佐野", "佐野恵太"),
         ("柴田", "柴田竜拓"), ("つつごう", "筒香嘉智"), ("筒香", "筒香嘉智"),
         ("関根", "関根大気"), ("戸柱", "戸柱恭孝"), ("神里", "神里和毅"),
         ("森", "森敬斗"), ("梶原", "梶原昂希"),
         ("阿部", None), ("大谷翔平", None)]


async def main():
    ok = fail = 0
    async with aiohttp.ClientSession() as s:
        songs = await server_tools._songs(s)
        for who, want in CASES:
            got = server_tools._find_player(songs, who)
            good = (got == want)
            print(("OK " if good else "NG ")
                  + f"{who} → {got}（想定 {want}）")
            ok, fail = (ok + 1, fail) if good else (ok, fail + 1)
    print(f"\n{ok}/{ok + fail}")
    return 1 if fail else 0


sys.exit(asyncio.run(main()))
