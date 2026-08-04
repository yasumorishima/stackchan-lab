"""声で応援歌を頼んで、返ってきた言葉を確かめる（本体のふりをして通す）。

2026-08-04 の指摘を試験にしてある:
  - 「宮崎」（普通の崎）と言っても「宮﨑敏郎」に当たること
  - 見つからない時に 25 件を読み上げないこと
  - 歌う道具を外したあとも、この経路が壊れていないこと

  ./.venv/bin/python test_song_e2e.py
"""
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import aiohttp

import app
import opus_codec

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))
UP_RATE, FRAME_MS = 16000, 60
ok = fail = 0


def check(name, cond, got=""):
    global ok, fail
    if cond:
        ok += 1
        print("OK %s %s" % (name, got))
    else:
        fail += 1
        print("NG %s %s" % (name, got))


async def speech(text):
    pcm = await app.openjtalk_tts(text)
    return app.resample_linear(pcm, app.DOWN_RATE, UP_RATE)


async def ask(text, device):
    """声で聞いて、読み上げられた文と鳴った長さを返す。"""
    pcm = await speech(text)
    quiet = bytes(2 * int(UP_RATE * 1.5))
    said, frames = [], 0
    headers = {"Protocol-Version": "1", "Device-Id": device,
               "Client-Id": "song-test"}
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect("ws://%s:%d/ws" % (HOST, PORT),
                                headers=headers) as ws:
            await ws.send_str(json.dumps({
                "type": "hello", "version": 1, "features": {"mcp": True},
                "transport": "websocket",
                "audio_params": {"format": "opus", "sample_rate": UP_RATE,
                                 "channels": 1, "frame_duration": FRAME_MS}}))
            hello = json.loads((await asyncio.wait_for(ws.receive(), 15)).data)
            assert hello["type"] == "hello", "server hello が来ない"
            enc = opus_codec.Encoder(UP_RATE, 1, FRAME_MS)
            await ws.send_str(json.dumps({"type": "listen", "state": "start",
                                          "mode": "auto"}))
            for p in enc.encode_stream(pcm + quiet):
                await ws.send_bytes(p)
                await asyncio.sleep(FRAME_MS / 1000.0)
            last = time.monotonic()
            end = time.monotonic() + 120
            while time.monotonic() < end:
                try:
                    msg = await asyncio.wait_for(ws.receive(), 4)
                except asyncio.TimeoutError:
                    if said and time.monotonic() - last > 6.0:
                        break
                    continue
                last = time.monotonic()
                if msg.type == aiohttp.WSMsgType.BINARY:
                    frames += 1
                    continue
                if msg.type != aiohttp.WSMsgType.TEXT:
                    break
                d = json.loads(msg.data)
                if d.get("type") == "stt":
                    print("  聞き取り: %s" % d.get("text"))
                elif d.get("type") == "tts" and d.get("state") \
                        == "sentence_start":
                    said.append(d.get("text", ""))
    return "".join(said), frames * FRAME_MS / 1000.0


async def main():
    print("「宮崎の応援歌を教えて」と頼む")
    text, secs = await ask("宮崎の応援歌を教えて", "aa:bb:cc:dd:ee:04")
    print("  返事: %s" % text[:100])
    check("普通の「崎」でも宮﨑敏郎に当たる",
          "みやざき" in text or "宮﨑" in text, "%.1f 秒鳴った" % secs)
    check("25 件を読み上げていない",
          "投手のテーマ" not in text and len(text) < 220,
          "%d 文字" % len(text))

    print("「いない選手の応援歌を教えて」と頼む")
    text2, _ = await ask("あべの応援歌を教えて", "aa:bb:cc:dd:ee:05")
    print("  返事: %s" % text2[:100])
    check("いない選手でも一覧を垂れ流さない",
          "投手のテーマ" not in text2 and len(text2) < 220,
          "%d 文字" % len(text2))

    print("\n%d/%d" % (ok, ok + fail))
    return 1 if fail else 0


sys.exit(asyncio.run(main()))
