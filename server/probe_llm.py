"""実 LLM のツール選択だけを切り出して測る。

app.py が組み立てるのと同じ tools 配列（サーバー側 SPECS + 本体 MCP の
マングリング済み名）を OpenAI 互換の chat completions に投げ、
返ってきた tool_calls と所要時間を並べる。音声経路は通さない。
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
TOKEN = os.environ.get("SAKURA_TOKEN", "dummy")

# 本体（1.4.4）が公開する唯一のツール。OpenAI 互換の関数名は "." を使えないので
# app.py と同じく "_" に置換した形で渡す。
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

SYSTEM = ("あなたはスタックちゃんという小さな卓上ロボットです。短く親しみやすく話します。"
          "現在時刻は %s です。" % time.strftime("%Y-%m-%d %H:%M"))

# (発話, 期待するツール名 or None, 期待する引数の部分一致)
CASES = [
    ("こんにちは。あなたの名前は何ですか。", None, {}),
    ("今日の天気を教えて。", "get_weather", {"when": "today"}),
    ("あしたの大阪の天気は？", "get_weather", {"place": "大阪", "when": "tomorrow"}),
    ("鳥取の天気を教えて。", "get_weather", {"place": "鳥取"}),
    ("音量を五十にして。", "self_audio_speaker_set_volume", {"volume": 50}),
    ("ボリュームを下げて、二十くらいに。", "self_audio_speaker_set_volume", {"volume": 20}),
    ("いまドル円いくら？", "get_usdjpy", {}),
    ("円安どうなってるか教えて。", "get_usdjpy", {}),
    ("日経平均いくら？", "get_stock_index", {"index": "nikkei"}),
    ("アメリカの株はどうなってる？", "get_stock_index", {}),
    ("今日の株価を教えて。", "get_stock_index", {}),
    ("無料枠あとどれくらい残ってる？", "get_llm_quota", {}),
    ("ありがとう、またね。", None, {}),
]


async def ask(session, messages):
    payload = {"model": MODEL, "messages": messages, "temperature": 0.7,
               "max_tokens": 200, "stream": False,
               "tools": TOOLS, "tool_choice": "auto"}
    t0 = time.monotonic()
    async with session.post(BASE + "/v1/chat/completions", json=payload,
                            headers={"Authorization": "Bearer " + TOKEN},
                            timeout=aiohttp.ClientTimeout(total=180)) as r:
        body = await r.json()
        if r.status != 200:
            raise RuntimeError("chat %d: %s" % (r.status, json.dumps(body)[:300]))
    return body["choices"][0]["message"], time.monotonic() - t0


async def main():
    n_ok = 0
    async with aiohttp.ClientSession() as s:
        for text, want_name, want_args in CASES:
            msgs = [{"role": "system", "content": SYSTEM},
                    {"role": "user", "content": text}]
            try:
                msg, dt = await ask(s, msgs)
            except Exception as e:
                print("FAIL %-28s -> 例外 %s" % (text, e))
                continue
            calls = msg.get("tool_calls") or []
            got_name = calls[0]["function"]["name"] if calls else None
            try:
                got_args = json.loads(calls[0]["function"]["arguments"] or "{}") if calls else {}
            except Exception:
                got_args = {"<parse error>": calls[0]["function"]["arguments"]}
            ok = (got_name == want_name)
            for k, v in want_args.items():
                if str(got_args.get(k)) != str(v):
                    ok = False
            n_ok += bool(ok)
            print("%-4s %-28s %5.1fs  tool=%s args=%s%s"
                  % ("OK" if ok else "NG", text, dt, got_name,
                     json.dumps(got_args, ensure_ascii=False),
                     "" if calls else "  content=" + (msg.get("content") or "")[:60]))
            if not ok:
                print("      期待: tool=%s args=%s" % (want_name, json.dumps(want_args, ensure_ascii=False)))
    print("\n%d/%d 正解  model=%s base=%s" % (n_ok, len(CASES), MODEL, BASE))


asyncio.run(main())
