"""口調の指示を 1 文足すとツール選択が落ちるかを、本番と同じ 34 ケースで測る。

probe_llm の CASES/TOOLS/ask をそのまま借り、system 文だけ差し替えて比べる。
"""
import asyncio
import json
import os
import sys

import aiohttp

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app  # noqa: E402
import probe_llm as P  # noqa: E402

EXTRA = "接客の決まり文句や絵文字は使いません。"


async def run(session, system):
    ok = 0
    ng = []
    for text, want_name, want_args in P.CASES:
        msgs = [{"role": "system", "content": system},
                {"role": "user", "content": text}]
        try:
            msg, _ = await P.ask(session, msgs)
        except Exception as e:
            ng.append((text, "例外 %s" % e))
            continue
        calls = msg.get("tool_calls") or []
        got_name = calls[0]["function"]["name"] if calls else None
        try:
            got_args = json.loads(calls[0]["function"]["arguments"] or "{}") if calls else {}
        except Exception:
            got_args = {}
        good = (got_name == want_name)
        for k, v in want_args.items():
            if str(got_args.get(k)) != str(v):
                good = False
        if good:
            ok += 1
        else:
            ng.append((text, "tool=%s args=%s" % (got_name, got_args)))
    return ok, ng


async def main():
    base = app.system_prompt()
    async with aiohttp.ClientSession() as s:
        for label, system in (("本番のまま", base), ("＋口調の指示", base + EXTRA)):
            ok, ng = await run(s, system)
            print("%s  %d/%d" % (label, ok, len(P.CASES)))
            for text, why in ng:
                print("   NG %s -> %s" % (text, why))


asyncio.run(main())
