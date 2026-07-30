"""3B で残った粗さ 2 点を、音声経路を通さずに測る。

  A. 省略形の追い質問で日付（when）を引き継げるか
     「あしたの大阪の天気は？」に答えた直後の「じゃあ鳥取はどう？」で
     get_weather(place=鳥取, when=tomorrow) に行けるか。
     履歴は app.py が実際に積むのと同じ形（ツール往復を含む）にする。
     モデル単体の点と、when を落とした時にサーバーが補った後の点を分けて出す。
  B. 「2 文以内」の指示を守れるか

temperature 0.7（app.py と同じ）なので 1 回では当たり外れが読めない。
既定で各ケース 3 回ずつ流して回数で出す（REPEAT で変えられる）。

  ./.venv/bin/python probe_followup.py
  SAKURA_MODEL=qwen2.5:7b REPEAT=3 ./.venv/bin/python probe_followup.py
"""
import asyncio
import json
import os
import re
import sys
import time

import aiohttp

sys.path.insert(0, "/home/yasu/stackchan-server")
import app  # noqa: E402
import server_tools  # noqa: E402

BASE = os.environ.get("SAKURA_BASE", "http://127.0.0.1:11434")
MODEL = os.environ.get("SAKURA_MODEL", "qwen2.5:3b")
REPEAT = int(os.environ.get("REPEAT", "3"))

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

STAMP = server_tools.jst_stamp()
FIRST_RESULT = "大阪市 7月31日(金) 晴れ時々くもり 最高32.8度 最低25.7度 降水確率41%"

# HISTORY で直前に調べた日。サーバー側の引き継ぎはこれを拾う
HISTORY_WHEN = "tomorrow"

# app.py が実際に積む形（stamped_user + assistant(tool_calls) + tool + assistant）
HISTORY = [
    {"role": "user", "content": "（" + STAMP + "）あしたの大阪の天気を教えて"},
    {"role": "assistant", "content": "",
     "tool_calls": [{"id": "call_1", "type": "function",
                     "function": {"name": "get_weather",
                                  "arguments": '{"place": "大阪", "when": "tomorrow"}'}}]},
    {"role": "tool", "tool_call_id": "call_1", "content": FIRST_RESULT},
    {"role": "assistant",
     "content": "あしたの大阪は晴れ時々くもりで、最高32.8度、最低25.7度です。降水確率は41%です。"},
]

FOLLOWUPS = [
    ("じゃあ鳥取はどう", {"place": "鳥取", "when": "tomorrow"}),
    ("鳥取は", {"place": "鳥取", "when": "tomorrow"}),
    ("今日はどうなの", {"place": "大阪", "when": "today"}),
]

LENGTH_CASES = [
    "こんにちは。あなたは何ができるの。",
    "ありがとう、またね。",
    "きょうも暑いね。",
]


async def ask(session, messages):
    payload = {"model": MODEL, "messages": messages, "temperature": 0.7,
               "max_tokens": 200, "stream": False,
               "tools": TOOLS, "tool_choice": "auto"}
    t0 = time.monotonic()
    async with session.post(BASE + "/v1/chat/completions", json=payload,
                            headers={"Authorization": "Bearer dummy"},
                            timeout=aiohttp.ClientTimeout(total=300)) as r:
        body = await r.json()
        if r.status != 200:
            raise RuntimeError("chat %d: %s" % (r.status, json.dumps(body)[:300]))
    return body["choices"][0]["message"], time.monotonic() - t0


def sentences(text):
    return [s for s in re.split(r"(?<=[。！？!?])", text or "") if s.strip()]


def first_call(msg):
    """最初のツール呼び出しの (名前, 引数)。呼んでいなければ (None, {})。"""
    calls = msg.get("tool_calls") or []
    if not calls:
        return None, {}
    fn = calls[0]["function"]
    try:
        return fn["name"], json.loads(fn.get("arguments") or "{}")
    except Exception:
        return fn["name"], {}


async def main():
    sysmsg = {"role": "system", "content": app.system_prompt()}
    tot_model = tot_sys = tot_len = tot_n = tot_spoken = 0
    async with aiohttp.ClientSession() as s:
        await ask(s, [sysmsg, {"role": "user", "content": "こんにちは。"}])  # warm

        print("== A. 省略形の追い質問で日付を引き継げるか（%d 回ずつ） ==" % REPEAT)
        for text, want in FOLLOWUPS:
            n_call = n_model = n_sys = 0
            worst = ""
            for _ in range(REPEAT):
                msgs = [sysmsg] + HISTORY + [
                    {"role": "user", "content": "（" + STAMP + "）" + text}]
                msg, dt = await ask(s, msgs)
                name, got = first_call(msg)
                n_call += name == "get_weather"
                n_model += name == "get_weather" and all(
                    str(got.get(k)) == str(v) for k, v in want.items())
                # サーバーの補い込み（app.py の call_tool と同じ文脈を渡す）
                eff = dict(got)
                if name == "get_weather" and \
                        eff.get("when") not in server_tools.WHEN_VALUES:
                    eff["when"] = server_tools.infer_when(
                        {"utterance": text, "last_when": HISTORY_WHEN})
                n_sys += name == "get_weather" and all(
                    str(eff.get(k)) == str(v) for k, v in want.items())
                if name != "get_weather":
                    worst = "ツール未呼び出し: " + (
                        app.clean_reply(msg.get("content")) or "(本文なし)")[:80]
                elif not all(str(eff.get(k)) == str(v) for k, v in want.items()):
                    worst = "補い込み後も不一致: " + json.dumps(
                        eff, ensure_ascii=False)
            print("  %-12s 呼び出し %d/%d  モデル正解 %d/%d  補い込み後 %d/%d  %s"
                  % (text, n_call, REPEAT, n_model, REPEAT, n_sys, REPEAT, worst))
            tot_model += n_model
            tot_sys += n_sys
            tot_n += REPEAT

        print("== B. 2 文以内（モデル素の文数 / 実際に読み上げる文数） ==")
        for text in LENGTH_CASES:
            n_ok = n_spoken_ok = 0
            sample = ""
            for _ in range(REPEAT):
                msgs = [sysmsg, {"role": "user", "content": "（" + STAMP + "）" + text}]
                msg, dt = await ask(s, msgs)
                raw = app.clean_reply(msg.get("content"))
                spoken = app.shorten_reply(raw)
                n_raw = len(sentences(raw))
                n_ok += 1 <= n_raw <= 2
                n_spoken_ok += len(sentences(spoken)) <= app.MAX_SENTENCES
                if n_raw > 2:
                    sample = "素%d文 -> 読み上げ: %s" % (n_raw, spoken[:70])
                elif not sample:
                    sample = "素%d文: %s" % (n_raw, spoken[:60])
            print("  %-18s モデル %d/%d  読み上げ %d/%d  %s"
                  % (text, n_ok, REPEAT, n_spoken_ok, REPEAT, sample))
            tot_len += n_ok
            tot_spoken += n_spoken_ok

    print("")
    print("A モデル単体 %d/%d  補い込み後 %d/%d   "
          "B モデル %d/%d  読み上げ %d/%d   model=%s"
          % (tot_model, tot_n, tot_sys, tot_n,
             tot_len, len(LENGTH_CASES) * REPEAT,
             tot_spoken, len(LENGTH_CASES) * REPEAT, MODEL))


asyncio.run(main())
