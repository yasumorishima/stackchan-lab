"""「人名＋歌って」のツール選択だけを実さくらで測る（probe_llm は import すると
自分の 7 ケースを回してしまうので、ask を書き写した自己完結版）。
  SAKURA_BASE=https://api.ai.sakura.ad.jp SAKURA_TOKEN=... で実行する。
"""
import asyncio, json, os, sys, time
sys.path.insert(0, "/home/yasu/stackchan-server")
import aiohttp, server_tools

BASE = os.environ.get("SAKURA_BASE", "http://127.0.0.1:11434")
MODEL = os.environ.get("SAKURA_MODEL", "gpt-oss-120b")
TOKEN = os.environ.get("SAKURA_TOKEN", "dummy")
TOOLS = list(server_tools.specs())
SYSTEM = ("あなたはスタックちゃんという小さな卓上ロボットです。短く親しみやすく話します。"
          "現在時刻は %s です。" % time.strftime("%Y-%m-%d %H:%M"))
CASES = [
    ("度会の歌歌って", "sing_cheer_song"),
    ("度会の応援歌歌って", "sing_cheer_song"),
    ("宮崎の応援歌を歌って", "sing_cheer_song"),
    ("京田の応援歌", "sing_cheer_song"),
    ("京田の応援歌の歌詞教えて", "get_cheer_song"),
]

async def main():
    ok = 0
    take = int(sys.argv[1]) if len(sys.argv) > 1 else len(CASES)
    async with aiohttp.ClientSession() as s:
        for text, want in CASES[:take]:
            payload = {"model": MODEL, "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": text}],
                "temperature": 0.7, "max_tokens": 200, "stream": False,
                "tools": TOOLS, "tool_choice": "auto"}
            t0 = time.monotonic()
            async with s.post(BASE + "/v1/chat/completions", json=payload,
                              headers={"Authorization": "Bearer " + TOKEN},
                              timeout=aiohttp.ClientTimeout(total=180)) as r:
                body = await r.json()
                if r.status != 200:
                    print("FAIL %s: %d %s" % (text, r.status, json.dumps(body)[:120]))
                    continue
            msg = body["choices"][0]["message"]
            calls = msg.get("tool_calls") or []
            got = calls[0]["function"]["name"] if calls else None
            args = calls[0]["function"]["arguments"] if calls else ""
            ok += got == want
            print("%-4s %-14s %4.1fs tool=%s args=%s%s" % ("OK" if got == want else "NG", text, time.monotonic() - t0, got, args, "" if calls else " content=" + (msg.get("content") or "")[:60]))
    print("%d/%d 正解" % (ok, take))

asyncio.run(main())
