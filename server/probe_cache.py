"""現在時刻の差し込み位置がプロンプトキャッシュに与える影響を測る対照実験。

A: 現行実装と同じ = システムプロンプトの末尾に時刻。tools はテンプレート上
   システム文の後ろに描画されるので、時刻が変わると tools 全体が再計算される。
B: システム文は固定し、時刻は直近のユーザー発話に添える。共通接頭辞
   （固定システム文 + tools）が毎回そのまま再利用できるはず。

同じ発話列を A/B で流し、往復時間を並べる。
"""
import asyncio
import json
import os
import sys
import time

import aiohttp

sys.path.insert(0, "/home/yasu/stackchan-server")
import server_tools  # noqa: E402

BASE = os.environ.get("SAKURA_BASE", "http://127.0.0.1:11434")
MODEL = os.environ.get("SAKURA_MODEL", "qwen2.5:3b")

DEVICE_TOOL = {
    "type": "function",
    "function": {
        "name": "self_audio_speaker_set_volume",
        "description": "スピーカーの音量を 0-100 で設定する",
        "parameters": {"type": "object",
                       "properties": {"volume": {"type": "integer"}},
                       "required": ["volume"]},
    },
}
TOOLS = list(server_tools.specs()) + [DEVICE_TOOL]

BASE_SYSTEM = ("あなたは卓上ロボット「スタックちゃん」です。親しみやすく、短く話します。"
               "返答は 2 文以内、読み上げるので記号や箇条書きは使いません。"
               "天気などの外の情報や機体の操作は、推測せず必ずツールを使って答えます。")

UTTERANCES = ["今日の天気を教えて。", "あしたの大阪の天気は？",
              "音量を五十にして。", "鳥取の天気を教えて。"]


async def one(session, messages):
    payload = {"model": MODEL, "messages": messages, "temperature": 0.7,
               "max_tokens": 200, "stream": False,
               "tools": TOOLS, "tool_choice": "auto"}
    t0 = time.monotonic()
    async with session.post(BASE + "/v1/chat/completions", json=payload,
                            headers={"Authorization": "Bearer dummy"},
                            timeout=aiohttp.ClientTimeout(total=300)) as r:
        body = await r.json()
        if r.status != 200:
            raise RuntimeError("chat %d: %s" % (r.status, json.dumps(body)[:200]))
    return body["choices"][0]["message"], time.monotonic() - t0


def stamp():
    # 現行の server_tools.jst_stamp() と同じく分まで動く値
    return server_tools.jst_stamp()


async def run(session, mode):
    times = []
    for text in UTTERANCES:
        if mode == "A":
            msgs = [{"role": "system",
                     "content": BASE_SYSTEM + " 現在は " + stamp() + " です。"},
                    {"role": "user", "content": text}]
        else:
            msgs = [{"role": "system", "content": BASE_SYSTEM},
                    {"role": "user",
                     "content": "（現在は " + stamp() + " です）\n" + text}]
        msg, dt = await one(session, msgs)
        calls = msg.get("tool_calls") or []
        name = calls[0]["function"]["name"] if calls else None
        times.append(dt)
        print("  %s %-22s %6.1fs  tool=%s" % (mode, text, dt, name))
        # 分をまたがせて時刻文字列を必ず変える（実運用の発話間隔を模す）
        await asyncio.sleep(61)
    return times


async def main():
    async with aiohttp.ClientSession() as s:
        # まず一度投げてモデルとテンプレートを温める（初回ロードを混ぜない）
        await one(s, [{"role": "system", "content": BASE_SYSTEM},
                      {"role": "user", "content": "こんにちは。"}])
        print("== A: 時刻をシステム文の末尾に置く（現行）==")
        a = await run(s, "A")
        print("== B: システム文は固定、時刻はユーザー発話に添える ==")
        b = await run(s, "B")
    avg = lambda x: sum(x) / len(x)
    print("\nA 平均 %.1fs / B 平均 %.1fs" % (avg(a), avg(b)))


asyncio.run(main())
