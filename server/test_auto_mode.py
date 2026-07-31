"""mode:auto（本体が listen stop を送らない）でも応答できるかを見る。

実機は listen start に mode:auto を付けて音声を流し続け、発話の終わりを
送ってこない。サーバー側で無音を見て切れなければ、永久に応答が始まらない。

  ./.venv/bin/python test_auto_mode.py
"""
import asyncio
import io
import json
import math
import os
import struct
import sys
import urllib.request
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import aiohttp

import opus_codec

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))
UP_RATE, FRAME_MS = 16000, 60
DEVICE_ID = "aa:bb:cc:dd:ee:01"
VOICEVOX = os.environ.get("VOICEVOX_URL", "http://127.0.0.1:50021")
SPEAKER = int(os.environ.get("VOICEVOX_SPEAKER", "3"))
TEXT = os.environ.get("TEST_TEXT", "こんにちは")


def voicevox_pcm(text):
    q = urllib.request.urlopen(urllib.request.Request(
        "%s/audio_query?speaker=%d&text=%s" % (VOICEVOX, SPEAKER,
                                               urllib.parse.quote(text)),
        method="POST"), timeout=30).read()
    body = json.loads(q)
    body["outputSamplingRate"] = UP_RATE
    body["outputStereo"] = False
    wav = urllib.request.urlopen(urllib.request.Request(
        "%s/synthesis?speaker=%d" % (VOICEVOX, SPEAKER),
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST"), timeout=60).read()
    with wave.open(io.BytesIO(wav), "rb") as w:
        return w.readframes(w.getnframes())


def silence(ms):
    return b"\x00\x00" * int(UP_RATE * ms / 1000)


async def main() -> int:
    import urllib.parse                      # noqa: F401  （voicevox_pcm で使う）
    pcm = voicevox_pcm(TEXT)
    print("送る音声: %.2fs（%s）" % (len(pcm) / 2 / UP_RATE, TEXT))

    headers = {"Protocol-Version": "1", "Device-Id": DEVICE_ID,
               "Client-Id": "auto-mode-test"}
    url = "ws://%s:%d/ws" % (HOST, PORT)
    got = {"stt": None, "tts_start": False, "tts_stop": False}

    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(url, headers=headers) as ws:
            await ws.send_str(json.dumps({
                "type": "hello", "version": 1, "features": {"mcp": True},
                "transport": "websocket",
                "audio_params": {"format": "opus", "sample_rate": UP_RATE,
                                 "channels": 1, "frame_duration": FRAME_MS}}))
            hello = json.loads((await asyncio.wait_for(ws.receive(), 10)).data)
            assert hello["type"] == "hello", "server hello が来ない"

            enc = opus_codec.Encoder(UP_RATE, 1, FRAME_MS)
            await ws.send_str(json.dumps({"type": "listen", "state": "start",
                                          "mode": "auto"}))
            # 発話 → 無音。listen stop は「送らない」のがこの試験の主眼
            for p in enc.encode_stream(pcm + silence(1500)):
                await ws.send_bytes(p)
                await asyncio.sleep(0.005)
            print("音声と無音を送った（listen stop は送っていない）")

            try:
                while not got["tts_stop"]:
                    msg = await asyncio.wait_for(ws.receive(), 45)
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue
                    d = json.loads(msg.data)
                    if d.get("type") == "mcp":
                        continue
                    print("<-", msg.data[:160])
                    if d.get("type") == "stt":
                        got["stt"] = d.get("text")
                    if d.get("type") == "tts":
                        if d.get("state") == "start":
                            got["tts_start"] = True
                        if d.get("state") == "stop":
                            got["tts_stop"] = True
            except asyncio.TimeoutError:
                print("応答が来ないまま時間切れ")

    print("---")
    print("認識結果      :", got["stt"])
    print("読み上げ start:", got["tts_start"], " stop:", got["tts_stop"])
    ok = bool(got["stt"]) and got["tts_start"] and got["tts_stop"]
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


sys.exit(asyncio.run(main()))
