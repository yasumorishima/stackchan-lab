"""相槌に返事をしないことを live サーバーで確かめる。

Open JTalk で「うん」を合成して mode:auto で流し、サーバーが stt / llm / tts の
どれも返してこないことを見る（ゲートは send_json より手前で返る）。

対照（普通の発話には返事が来る）は test_auto_mode.py で見る。sherpa は
Open JTalk の機械声を認識できず何を言っても「うん」に潰れる（2026-08-01 実測。
×3 増幅のクリップでも同じ）ので、対照の合成には VOICEVOX が要る。

  ./.venv/bin/python probe_filler.py
"""
import asyncio
import io
import json
import os
import struct
import subprocess
import sys
import tempfile
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import aiohttp

import opus_codec

UP_RATE, FRAME_MS = 16000, 60
DEVICE_ID = "aa:bb:cc:dd:ee:03"
OTA = os.environ.get("OTA_URL", "http://127.0.0.1:8000/xiaozhi/ota/")
OJT_BIN = os.environ.get("OPENJTALK_BIN", "open_jtalk")
OJT_DIC = os.environ.get("OPENJTALK_DIC",
                         "/var/lib/mecab/dic/open-jtalk/naist-jdic")
OJT_VOICE = os.environ.get(
    "OPENJTALK_VOICE",
    "/usr/share/hts-voice/nitech-jp-atr503-m001/nitech_jp_atr503_m001.htsvoice")


def synth(text: str) -> bytes:
    """Open JTalk で 16kHz mono PCM を作り、VAD に届く音量へ整える。"""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        out = f.name
    try:
        subprocess.run([OJT_BIN, "-x", OJT_DIC, "-m", OJT_VOICE,
                        "-s", str(UP_RATE), "-ow", out],
                       input=text.encode("utf-8"), check=True, timeout=30)
        with wave.open(out, "rb") as w:
            pcm = w.readframes(w.getnframes())
    finally:
        os.unlink(out)
    vals = struct.unpack("<%dh" % (len(pcm) // 2), pcm)
    # クリップさせない。×3 固定で増幅したら歪んで sherpa が「うん」と
    # 誤認識した（rms 2 万超）。VAD に届かない時だけ目標 rms まで持ち上げる
    rms = (sum(v * v for v in vals) / max(1, len(vals))) ** 0.5
    k = min(2.0, 1500.0 / rms) if rms < 1500 else 1.0
    scaled = [max(-32768, min(32767, int(v * k))) for v in vals]
    return struct.pack("<%dh" % len(scaled), *scaled)


def silence(ms: int) -> bytes:
    return b"\x00\x00" * int(UP_RATE * ms / 1000)


async def send_utterance(ws, pcm: bytes):
    enc = opus_codec.Encoder(UP_RATE, 1, FRAME_MS)
    body = silence(300) + pcm + silence(1500)
    n = 0
    for p in enc.encode_stream(body):
        await ws.send_bytes(p)
        n += 1
    return n


async def collect(ws, seconds: float):
    got = []
    try:
        while True:
            msg = await asyncio.wait_for(ws.receive(), seconds)
            if msg.type == aiohttp.WSMsgType.TEXT:
                d = json.loads(msg.data)
                got.append(d.get("type"))
            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED,
                              aiohttp.WSMsgType.ERROR):
                break
    except asyncio.TimeoutError:
        pass
    return got


async def main() -> int:
    async with aiohttp.ClientSession() as s:
        payload = {"application": {"version": "1.4.4", "name": "stackchan"},
                   "board": {"type": "m5stack-stack-chan", "mac": DEVICE_ID}}
        async with s.post(OTA, json=payload,
                          headers={"Device-Id": DEVICE_ID}) as r:
            ota = await r.json()
        ws_url = ota["websocket"]["url"].replace("192.168.10.111", "127.0.0.1")
        headers = {"Authorization": "Bearer " + ota["websocket"]["token"],
                   "Protocol-Version": "1", "Device-Id": DEVICE_ID,
                   "Client-Id": "filler-probe"}
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

            # 相槌: 何も返ってこないこと
            n = await send_utterance(ws, synth("うん"))
            print("sent うん (%d frames)" % n)
            got = await collect(ws, 8)
            quiet = not any(t in ("stt", "llm", "tts") for t in got)
            print("相槌への応答: %s (期待 無し) -> %s"
                  % (got or "無し", "OK" if quiet else "NG"))

    print("RESULT:", "PASS" if quiet else "FAIL")
    return 0 if quiet else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
