"""ストリーミングで「最初の欠片・1 文目・全部」が何秒で来るかを測る。"""
import asyncio
import os
import sys
import time

import aiohttp

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app  # noqa: E402
import server_tools  # noqa: E402

ASK = chr(34)+chr(33509)+chr(12375)+chr(12356)+chr(35441)+chr(12434)+chr(19968)+chr(12388)+chr(32862)+chr(12363)+chr(12379)+chr(12390)+chr(12290)+chr(34)
BASE = os.environ.get("SAKURA_BASE", "https://api.ai.sakura.ad.jp")
MODEL = os.environ.get("SAKURA_MODEL", "gpt-oss-120b")
TOKEN = os.environ.get("SAKURA_TOKEN", "dummy")


async def main():
    tools = list(server_tools.specs())
    async with aiohttp.ClientSession() as s:
        for i in range(3):
            t0 = time.monotonic()
            marks = {}

            async def on_delta(text, _t0=t0, _m=marks):
                if "first" not in _m:
                    _m["first"] = time.monotonic() - _t0
                if "sentence" not in _m and any(c in text for c in "。！？"):
                    _m["sentence"] = time.monotonic() - _t0

            msg = await app.chat_once(s, [{"role": "user", "content": ASK}],
                                      tools, BASE, MODEL, TOKEN,
                                      on_delta=on_delta)
            whole = time.monotonic() - t0
            body = (msg.get("content") or "").replace("\n", " ")
            print("%d 回目  最初の欠片 %.2fs / 1 文目 %s / 全部 %.2fs  文字数 %d"
                  % (i + 1, marks.get("first", -1),
                     ("%.2fs" % marks["sentence"]) if "sentence" in marks else "-",
                     whole, len(body)))


asyncio.run(main())
