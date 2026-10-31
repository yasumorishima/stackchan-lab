"""生バイトタップの動作確認。hello だけ送って server hello を受けたら閉じる。

STT/LLM/TTS は一切通らないので RPi5 に負荷をかけない。
"""
import asyncio
import json
import sys

import aiohttp

URL = "ws://127.0.0.1:8000/ws"
HELLO = {
    "type": "hello", "version": 1, "features": {"mcp": True},
    "transport": "websocket",
    "audio_params": {"format": "opus", "sample_rate": 16000,
                     "channels": 1, "frame_duration": 60},
}


async def main() -> int:
    headers = {"Protocol-Version": "1", "Device-Id": "aa:bb:cc:dd:ee:ff",
               "Client-Id": "probe-rawtap"}
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(URL, headers=headers) as ws:
            await ws.send_str(json.dumps(HELLO))
            msg = await asyncio.wait_for(ws.receive(), timeout=5)
            data = json.loads(msg.data)
            ok = data.get("type") == "hello" and data.get("transport") == "websocket"
            print("server hello:", msg.data)
            await ws.close()
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


sys.exit(asyncio.run(main()))
