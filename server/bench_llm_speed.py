"""さくらの AI Engine のチャットモデルを、返事の速さで比べる（本番の Pi で回す）。

server.conf の SAKURA_BASE / SAKURA_TOKEN を使う。トークンは表示しない。
本番と同じ指示文・道具一覧を付けて、モデルごとに 2 問 × 3 回ずつ投げ、
「最初の 1 文字（または道具の呼び出し）が届くまで」と「全部届くまで」を測る。

  ./.venv/bin/python bench_llm_speed.py

無償枠で動かすのが絶対なので、測るのは gpt-oss-120b だけ（考える量 low と既定）。
"""
import asyncio
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import aiohttp

import app
import server_tools

BASE = os.environ.get("SAKURA_BASE", "https://api.ai.sakura.ad.jp")
TOKEN = os.environ.get("SAKURA_TOKEN", "")
TRIES = int(os.environ.get("TRIES", "3"))
# 名前にこれを含むモデルを候補にする（埋め込み・音声・画像専用は外す）
WANT = ("gpt-oss", "Qwen3.6", "Qwen3-VL", "gemma", "llm-jp", "Phi-4", "Qwen3-0.6B")
SKIP = ("Embedding", "e5", "whisper", "Med", "Code")
QUESTIONS = [("雑談", "スタックちゃん、今日の調子はどう？"),
             ("道具", "横浜の今日の天気は？")]


async def models(s):
    async with s.get(BASE + "/v1/models",
                     headers={"Authorization": "Bearer " + TOKEN}) as r:
        body = await r.json(content_type=None)
    ids = [m.get("id") for m in body.get("data", [])]
    print("一覧:", ", ".join(ids))
    return [i for i in ids if any(w in i for w in WANT)
            and not any(k in i for k in SKIP)]


async def once(s, model, text, effort):
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": app.system_prompt()},
                     {"role": "user", "content": text}],
        "max_tokens": 800, "stream": True,
        "tools": server_tools.specs(), "tool_choice": "auto",
    }
    if effort:
        payload["reasoning_effort"] = effort
    t0 = time.monotonic()
    first = None
    kind = ""
    out = ""
    async with s.post(BASE + "/v1/chat/completions", json=payload,
                      headers={"Authorization": "Bearer " + TOKEN},
                      timeout=aiohttp.ClientTimeout(total=60)) as r:
        if r.status != 200:
            return None, None, "HTTP %d %s" % (r.status, (await r.text())[:120])
        async for raw in r.content:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:") or line == "data: [DONE]":
                continue
            try:
                d = json.loads(line[5:])
            except Exception:
                continue
            for c in d.get("choices") or []:
                delta = c.get("delta") or {}
                if first is None and (delta.get("content") or
                                      delta.get("tool_calls")):
                    first = time.monotonic() - t0
                    kind = "道具" if delta.get("tool_calls") else "本文"
                if delta.get("content"):
                    out += delta["content"]
                for tc in delta.get("tool_calls") or []:
                    fn = (tc.get("function") or {}).get("name")
                    if fn:
                        out += "[%s]" % fn
    return first, time.monotonic() - t0, "%s: %s" % (kind, out[:40])


async def main():
    if not TOKEN:
        print("SAKURA_TOKEN が無い")
        return 1
    async with aiohttp.ClientSession() as s:
        # 🔴 無償枠で動かすのが絶対（user 2026-11-05）。無償枠があるのは
        # gpt-oss-120b だけなので、ほかのモデルには投げない
        await models(s)
        cands = ["gpt-oss-120b"]
        runs = []
        for m in cands:
            for effort in (["low", None] if "gpt-oss" in m else [None]):
                runs.append((m, effort))
        print("%-34s %-6s %-4s %8s %8s  %s" % ("モデル", "考え", "問", "最初", "全部",
                                             "例"))
        for m, effort in runs:
            for label, q in QUESTIONS:
                firsts, totals, note = [], [], ""
                for _ in range(TRIES):
                    try:
                        f, t, note = await once(s, m, q, effort)
                    except Exception as e:
                        f, t, note = None, None, "%s" % type(e).__name__
                    if f is not None:
                        firsts.append(f)
                        totals.append(t)
                med = (lambda v: "%.2fs" % statistics.median(v) if v else "-")
                print("%-34s %-6s %-4s %8s %8s  %s" % (
                    m, effort or "既定", label, med(firsts), med(totals), note))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
