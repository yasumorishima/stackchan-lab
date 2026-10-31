"""本文が max_tokens で途中終了する件を、実機ログの再現で測る。

2026-08-03 06:54 実機: 「さっ一週間の天気」→ get_weather を 2 回呼んだあと
本文が「…最高気温」で終わった（読み上げも途中で終わった）。
12:42 実機: get_sky の答えが「日の出は、04:52 頃です。」で終わり、
日の入り・月齢が落ちた。

ここでは同じ履歴を組み、max_tokens ごとに finish_reason と
本文の長さ・reasoning の消費トークンを出す。

  SAKURA_BASE=https://api.ai.sakura.ad.jp ./.venv/bin/python probe_length.py
"""
import asyncio
import json
import os
import sys

import aiohttp

sys.path.insert(0, "/home/yasu/stackchan-server")
import app  # noqa: E402
import server_tools  # noqa: E402

BASE = os.environ.get("SAKURA_BASE", "https://api.ai.sakura.ad.jp")
MODEL = os.environ.get("SAKURA_MODEL", "gpt-oss-120b")
TOKEN = os.environ.get("SAKURA_TOKEN", "")
LIMITS = [int(x) for x in os.environ.get("LIMITS", "400,900").split(",")]

CASES = [
    ("week-2tools", "さっ一週間の天気", [
        ("get_weather", {"when": "tomorrow"},
         "横浜市 8月4日(火) 晴れ時々くもり 最高26.9度 最低21.5度 降水確率24%"),
        ("get_weather", {"when": "day_after_tomorrow"},
         "横浜市 8月5日(水) 晴れ時々くもり 最高28.2度 最低22.5度 降水確率29%"),
    ]),
    ("sky", "では今日の空を見るポイントも確認していきま", [
        ("get_sky", {},
         "きょうの月齢はおよそ19.1で、満月をすぎて欠けはじめた月です。"
         "横浜の日の出は4時52分、日の入りは18時45分です。"
         "日の入りまであと6.1時間です。"),
    ]),
    ("fuel-all", "燃油サーチャージは", [
        ("get_fuel_surcharge", {"destination": ""},
         "8月31日ご購入分まで、ANAの日本発、片道の燃油サーチャージです。"
         "欧州・北米・中東・オセアニアが65,000円、"
         "ハワイ・インド・インドネシアが40,400円、"
         "タイ・シンガポール・マレーシア・ミャンマー・カンボジアが33,500円、"
         "ベトナム・グアム・フィリピン・パラオ・モンゴルが22,500円、"
         "東アジアが15,400円、韓国・ロシアが7,400円です。"),
    ]),
]


def history_for(case):
    _, utter, calls = case
    hist = [{"role": "user", "content": utter}]
    tool_calls = []
    for i, (name, args, _res) in enumerate(calls):
        tool_calls.append({
            "id": "call_%d" % i, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        })
    hist.append({"role": "assistant", "content": "", "tool_calls": tool_calls})
    for i, (name, _args, res) in enumerate(calls):
        hist.append({"role": "tool", "tool_call_id": "call_%d" % i,
                     "name": name, "content": res})
    return hist


async def ask(session, hist, limit):
    payload = {
        "model": MODEL,
        "messages": [{"role": "system", "content": app.system_prompt()}] + hist,
        "temperature": 0.7,
        "max_tokens": limit,
        "stream": False,
        "tools": list(server_tools.specs()),
        "tool_choice": "auto",
    }
    headers = {"Authorization": "Bearer " + TOKEN,
               "Content-Type": "application/json"}
    async with session.post(BASE + "/v1/chat/completions", json=payload,
                            headers=headers,
                            timeout=aiohttp.ClientTimeout(total=120)) as r:
        body = await r.json()
    ch = body["choices"][0]
    msg = ch["message"]
    return ch.get("finish_reason"), (msg.get("content") or ""), body.get("usage", {})


async def main():
    async with aiohttp.ClientSession() as session:
        for case in CASES:
            for limit in LIMITS:
                fin, text, usage = await ask(session, history_for(case), limit)
                spoken = app.shorten_reply(app.clean_reply(text))
                print("=== %s max_tokens=%d finish=%s 生成=%s 本文%d字 読み上げ%d字"
                      % (case[0], limit, fin,
                         usage.get("completion_tokens"), len(text), len(spoken)))
                print("   本文: " + text.replace("\n", " ")[:200])
                print("   読み: " + spoken[:200])


asyncio.run(main())
