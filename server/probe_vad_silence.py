"""mode:auto で無音だけを流し、サーバーが STT へ回さず掃き出すことを見る。

2026-08-01 の OOM 再発防止（vad_step の buffered_ms 上限）の live 確認用。
300 フレーム（18 秒ぶん）の無音を送る。修正後は 250 フレーム（15 秒）で
「無音バッファ …を捨てて聞き直す」がログに出て、stt は一度も走らない。

  ./.venv/bin/python probe_vad_silence.py
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import aiohttp

import opus_codec

UP_RATE, FRAME_MS = 16000, 60
DEVICE_ID = "aa:bb:cc:dd:ee:02"
OTA = os.environ.get("OTA_URL", "http://127.0.0.1:8000/xiaozhi/ota/")
FRAMES = 300


async def main():
    async with aiohttp.ClientSession() as s:
        payload = {"application": {"version": "1.4.4", "name": "stackchan"},
                   "board": {"type": "m5stack-stack-chan", "mac": DEVICE_ID}}
        async with s.post(OTA, json=payload,
                          headers={"Device-Id": DEVICE_ID}) as r:
            ota = await r.json()
        ws_url = ota["websocket"]["url"].replace("192.168.10.111", "127.0.0.1")
        headers = {"Authorization": "Bearer " + ota["websocket"]["token"],
                   "Protocol-Version": "1", "Device-Id": DEVICE_ID,
                   "Client-Id": "vad-silence-probe"}
        async with s.ws_connect(ws_url, headers=headers) as ws:
            await ws.send_str(json.dumps({
                "type": "hello", "version": 1, "features": {},
                "transport": "websocket",
                "audio_params": {"format": "opus", "sample_rate": UP_RATE,
                                 "channels": 1, "frame_duration": FRAME_MS}}))
            hello = json.loads((await asyncio.wait_for(ws.receive(), 10)).data)
            assert hello["type"] == "hello", hello
            await ws.send_str(json.dumps({"type": "listen", "state": "start",
                                          "mode": "auto"}))
            enc = opus_codec.Encoder(UP_RATE, 1, FRAME_MS)
            pcm = b"\x00\x00" * (int(UP_RATE * FRAME_MS / 1000) * FRAMES)
            n = 0
            for p in enc.encode_stream(pcm):
                await ws.send_bytes(p)
                n += 1
            print("sent %d silent frames (%.1fs)" % (n, n * FRAME_MS / 1000))
            # サーバーが掃き出しを処理する時間を少しだけ与える
            await asyncio.sleep(2)
    print("done（判定はサーバーログで行う）")


if __name__ == "__main__":
    asyncio.run(main())
