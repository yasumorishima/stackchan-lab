"""本番 LLM が駄目な時にローカルへ落ちるかを、実推論なしで確かめる。

その場に小さな OpenAI 互換サーバーを立てて「ローカル側」の役をさせるので、
ollama も本体も要らない（RPi5 に負荷をかけないため実推論は使わない）。

  ./.venv/bin/python test_fallback.py
"""
import asyncio
import sys

sys.path.insert(0, "/home/yasu/stackchan-server")

import aiohttp
from aiohttp import web

import app

CANNED = "ローカルのモデルが答えました。"
DEAD = "http://127.0.0.1:9"        # 誰も listen していない
calls = {"local": 0, "primary": 0}


async def local_chat(request):
    calls["local"] += 1
    await request.json()
    return web.json_response(
        {"choices": [{"message": {"role": "assistant", "content": CANNED}}]})


def make_primary(status, body=None):
    async def handler(request):
        calls["primary"] += 1
        await request.json()
        return web.json_response(body or {"error": "ng"}, status=status)
    return handler


async def serve(handler, port):
    a = web.Application()
    a.router.add_post("/v1/chat/completions", handler)
    runner = web.AppRunner(a)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    return runner


async def main():
    ok = True

    def check(memo, got, want):
        nonlocal ok
        good = got == want
        if not good:
            ok = False
        print("%-4s %-44s -> %s（期待 %s）"
              % ("OK" if good else "NG", memo, got, want))

    # ローカル役を 18100 に立てる
    local = await serve(local_chat, 18100)
    app.FALLBACK_LLM_BASE = "http://127.0.0.1:18100"
    app.FALLBACK_LLM_MODEL = "test-local"
    hist = [{"role": "user", "content": "こんにちは"}]

    async with aiohttp.ClientSession() as s:
        # ① 本番へ繋がらない（ネット断）→ ローカルへ落ちる
        app.SAKURA_BASE = DEAD
        calls["local"] = 0
        app._primary_down_until = 0.0   # 前のケースを持ち越さない
        msg = await sakura_or_error(s, hist)
        check("繋がらない時はローカルへ落ちる", msg.get("content"), CANNED)
        check("  ローカルを 1 回だけ呼ぶ", calls["local"], 1)

        # ② 本番が 503（向こうの都合）→ ローカルへ落ちる
        prim = await serve(make_primary(503), 18101)
        app.SAKURA_BASE = "http://127.0.0.1:18101"
        app._primary_down_until = 0.0   # 前のケースを持ち越さない
        calls["local"] = calls["primary"] = 0
        msg = await sakura_or_error(s, hist)
        check("503 ならローカルへ落ちる", msg.get("content"), CANNED)
        check("  本番も 1 回試している", calls["primary"], 1)
        await prim.cleanup()

        # ③ 本番が 401（トークン切れ）→ ローカルへ落ちる
        prim = await serve(make_primary(401), 18102)
        app.SAKURA_BASE = "http://127.0.0.1:18102"
        app._primary_down_until = 0.0   # 前のケースを持ち越さない
        calls["local"] = 0
        msg = await sakura_or_error(s, hist)
        check("401 ならローカルへ落ちる", msg.get("content"), CANNED)
        await prim.cleanup()

        # ④ 本番が 400（自分の組み立てが悪い）→ 落ちずにそのまま上げる
        prim = await serve(make_primary(400), 18103)
        app.SAKURA_BASE = "http://127.0.0.1:18103"
        app._primary_down_until = 0.0   # 前のケースを持ち越さない
        calls["local"] = 0
        got = await sakura_or_error(s, hist)
        check("400 は落ちずに error", isinstance(got, str) and "400" in got, True)
        check("  ローカルは呼ばない", calls["local"], 0)
        await prim.cleanup()

        # ⑤ フォールバックを空にすると無効
        app.SAKURA_BASE = DEAD
        app.FALLBACK_LLM_BASE = ""
        app._primary_down_until = 0.0   # 前のケースを持ち越さない
        calls["local"] = 0
        got = await sakura_or_error(s, hist)
        check("空なら無効（error になる）", isinstance(got, str), True)
        check("  ローカルは呼ばない", calls["local"], 0)
        app.FALLBACK_LLM_BASE = "http://127.0.0.1:18100"

        # ⑥ 本番とローカルが同じ宛先なら二度撃たない
        app.SAKURA_BASE = "http://127.0.0.1:18100"
        calls["local"] = 0
        app._primary_down_until = 0.0   # 前のケースを持ち越さない
        msg = await sakura_or_error(s, hist)
        check("同じ宛先なら 1 回だけ", calls["local"], 1)

        # (7) 本番が駄目な間は本番を叩き直さない（毎回 timeout ぶん黙るのを防ぐ）
        prim = await serve(make_primary(503), 18104)
        app.SAKURA_BASE = "http://127.0.0.1:18104"
        app._primary_down_until = 0.0
        calls["local"] = calls["primary"] = 0
        await sakura_or_error(s, hist)
        await sakura_or_error(s, hist)
        await sakura_or_error(s, hist)
        check("3 回話しても本番は 1 回だけ試す", calls["primary"], 1)
        check("  ローカルは 3 回答える", calls["local"], 3)
        check("  クールダウンが立っている", app._primary_down_until > 0, True)

        # (8) クールダウンが切れたら本番を試し直し、戻っていれば本番を使う
        await prim.cleanup()
        back = await serve(local_chat, 18105)   # 本番役が復活した想定
        app.SAKURA_BASE = "http://127.0.0.1:18105"
        app._primary_down_until = 0.0
        calls["local"] = 0
        msg = await sakura_or_error(s, hist)
        check("復活後は本番を使う", calls["local"], 1)
        check("  クールダウンは解除", app._primary_down_until, 0.0)
        await back.cleanup()
        app.SAKURA_BASE = DEAD

    # should_fall_back の判定（通信なし）
    for status, want in ((401, True), (403, True), (429, True), (500, True),
                         (503, True), (400, False), (404, False), (422, False)):
        check("should_fall_back(%d)" % status,
              app.should_fall_back(app.ChatHTTPError(status, {})), want)
    check("接続不能などは落ちる",
          app.should_fall_back(OSError("connection refused")), True)

    await local.cleanup()
    print("")
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


async def sakura_or_error(session, hist):
    """例外は文字列にして返す（試験の中で分岐しやすくするため）。"""
    try:
        return await app.sakura_chat(session, hist)
    except Exception as e:
        return "error: %s" % e


sys.exit(asyncio.run(main()))
