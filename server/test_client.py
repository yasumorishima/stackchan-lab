"""本体（CoreS3）を模した試験クライアント。

実機なしで OTA -> WebSocket -> hello -> 音声送信 -> 応答受信 の一連を検証する。
本体と同じ条件（Opus 16000Hz mono 60ms、hello の形、Device-Id ヘッダ）で叩く。
"""
import asyncio
import io as _io
import json
import math
import os
import struct
import sys
import urllib.parse
import urllib.request
import wave

import aiohttp

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import opus_codec  # noqa: E402

OTA = os.environ.get("OTA_URL", "http://127.0.0.1:8000/xiaozhi/ota/")
DEVICE_ID = os.environ.get("DEVICE_ID", "aa:bb:cc:dd:ee:ff")   # 本体の Wi-Fi MAC
UP_RATE = 16000
FRAME_MS = 60


TEST_TEXT = os.environ.get("TEST_TEXT", "")
VOICEVOX_URL = os.environ.get("VOICEVOX_URL", "http://127.0.0.1:50021")
VOICEVOX_SPEAKER = int(os.environ.get("TEST_SPEAKER", "3"))


def voicevox_pcm(text, rate=UP_RATE):
    """VOICEVOX で本物の日本語音声を作り、本体マイク相当の 16k PCM にする。

    合成音なので人の声そのものではないが、STT/LLM/TTS を通した往復を
    ダミー波形でなく実発話で検証できる。
    """
    def post(path, params, body=None):
        url = VOICEVOX_URL + path + "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode("utf-8") if body is not None else b""
        req = urllib.request.Request(url, data=data, method="POST")
        if body is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.read()

    q = json.loads(post("/audio_query", {"text": text, "speaker": VOICEVOX_SPEAKER}))
    q["outputSamplingRate"] = rate
    q["outputStereo"] = False
    with wave.open(_io.BytesIO(post("/synthesis", {"speaker": VOICEVOX_SPEAKER}, q)), "rb") as w:
        assert w.getframerate() == rate and w.getnchannels() == 1
        return w.readframes(w.getnframes())


FAKE_TOOLS = [{
    "name": "self.audio_speaker.set_volume",
    "description": "スピーカーの音量を 0-100 で設定する",
    "inputSchema": {"type": "object",
                    "properties": {"volume": {"type": "integer"}},
                    "required": ["volume"]},
}]


def mcp_device_reply(payload):
    """本体の MCP サーバー役。initialize / tools/list / tools/call に答える。"""
    method = payload.get("method")
    rid = payload.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid,
                "result": {"protocolVersion": "2024-11-05",
                           "capabilities": {"tools": {}},
                           "serverInfo": {"name": "m5stack-stack-chan",
                                          "version": "1.4.4"}}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid,
                "result": {"tools": FAKE_TOOLS, "nextCursor": ""}}
    if method == "tools/call":
        params = payload.get("params") or {}
        print("   [device] tools/call %s %s" % (params.get("name"), params.get("arguments")))
        return {"jsonrpc": "2.0", "id": rid,
                "result": {"content": [{"type": "text", "text": "true"}],
                           "isError": False}}
    return None


async def drain(ws, seconds):
    """待っている間に届くメッセージを処理する（MCP の初期化とツール一覧に答える）。"""
    end = asyncio.get_running_loop().time() + seconds
    while True:
        left = end - asyncio.get_running_loop().time()
        if left <= 0:
            return
        try:
            msg = await asyncio.wait_for(ws.receive(), left)
        except asyncio.TimeoutError:
            return
        if msg.type != aiohttp.WSMsgType.TEXT:
            continue
        d = json.loads(msg.data)
        print("<-", msg.data[:160])
        if d.get("type") == "mcp":
            rep = mcp_device_reply(d.get("payload") or {})
            if rep is not None:
                await ws.send_str(json.dumps({"type": "mcp", "payload": rep},
                                             ensure_ascii=False))


def upstream_pcm():
    if TEST_TEXT:
        pcm = voicevox_pcm(TEST_TEXT)
        print("upstream: VOICEVOX 音声 %.2fs / %s" % (len(pcm) / 2 / UP_RATE, TEST_TEXT))
        return pcm
    return speech_like_pcm()


def speech_like_pcm(seconds=1.5, rate=UP_RATE):
    """無音だと短すぎ判定に落ちるので、それらしい波形を作る。"""
    n = int(rate * seconds)
    out = []
    for i in range(n):
        t = i / rate
        v = (math.sin(2 * math.pi * 180 * t) * 0.5
             + math.sin(2 * math.pi * 400 * t) * 0.3
             + math.sin(2 * math.pi * 900 * t) * 0.2)
        v *= 0.5 + 0.5 * math.sin(2 * math.pi * 3 * t)
        out.append(int(9000 * v))
    return struct.pack("<%dh" % n, *out)


async def main():
    ok = True
    async with aiohttp.ClientSession() as s:
        # 1. OTA
        payload = {"application": {"version": "1.4.4", "name": "stackchan"},
                   "board": {"type": "m5stack-stack-chan", "mac": DEVICE_ID}}
        async with s.post(OTA, json=payload,
                          headers={"Device-Id": DEVICE_ID,
                                   "Content-Type": "application/json"}) as r:
            ota = await r.json()
        print("OTA status:", r.status)
        print("OTA body  :", json.dumps(ota, ensure_ascii=False))
        assert ota["firmware"]["version"] == "1.4.4", "現在版と同じ version を返すこと"
        ws_url = ota["websocket"]["url"].replace("192.168.10.111", "127.0.0.1")
        token = ota["websocket"]["token"]

        # 2. WebSocket
        headers = {"Authorization": "Bearer " + token,
                   "Protocol-Version": "1",
                   "Device-Id": DEVICE_ID,
                   "Client-Id": "test-client-0001"}
        async with s.ws_connect(ws_url, headers=headers) as ws:
            await ws.send_str(json.dumps({
                "type": "hello", "version": 1,
                "features": {"mcp": True},
                "transport": "websocket",
                "audio_params": {"format": "opus", "sample_rate": UP_RATE,
                                 "channels": 1, "frame_duration": FRAME_MS},
            }))
            hello = json.loads((await asyncio.wait_for(ws.receive(), 10)).data)
            print("server hello:", json.dumps(hello, ensure_ascii=False))
            assert hello["type"] == "hello", "hello が返ること"
            assert hello["transport"] == "websocket", "transport は websocket 厳密一致"
            down_rate = hello["audio_params"]["sample_rate"]

            # 3. 発話の前に、本体と同じく届いたメッセージへ答えておく（MCP 初期化）
            await drain(ws, 3.0)

            # 4. 発話を送る
            enc = opus_codec.Encoder(UP_RATE, 1, FRAME_MS)
            await ws.send_str(json.dumps({"type": "listen", "state": "start",
                                          "mode": "auto"}))
            frames = list(enc.encode_stream(upstream_pcm()))
            for p in frames:
                await ws.send_bytes(p)
            print("sent %d opus frames (%.1fs)" % (len(frames), len(frames) * FRAME_MS / 1000))
            await ws.send_str(json.dumps({"type": "listen", "state": "stop"}))

            # 4. 応答を受ける
            dec = opus_codec.Decoder(down_rate, 1)
            got = {"stt": False, "tts_start": False, "tts_stop": False}
            audio_bytes = 0
            audio_frames = 0
            try:
                while not got["tts_stop"]:
                    msg = await asyncio.wait_for(ws.receive(), 45)
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        d = json.loads(msg.data)
                        print("<-", msg.data[:200])
                        if d.get("type") == "mcp":
                            rep = mcp_device_reply(d.get("payload") or {})
                            if rep is not None:
                                await ws.send_str(json.dumps(
                                    {"type": "mcp", "payload": rep}, ensure_ascii=False))
                                got["mcp"] = True
                        if d.get("type") == "stt":
                            got["stt"] = True
                        if d.get("type") == "tts" and d.get("state") == "start":
                            got["tts_start"] = True
                        if d.get("type") == "tts" and d.get("state") == "stop":
                            got["tts_stop"] = True
                    elif msg.type == aiohttp.WSMsgType.BINARY:
                        pcm = dec.decode(msg.data)   # 復号できることが検証点
                        audio_bytes += len(pcm)
                        audio_frames += 1
                    else:
                        print("closed:", msg.type)
                        break
            except asyncio.TimeoutError:
                print("TIMEOUT")
                ok = False

            print("---")
            print("stt received      :", got["stt"])
            print("tts start/stop    :", got["tts_start"], got["tts_stop"])
            print("audio frames      :", audio_frames)
            print("decoded audio     : %.2f s @ %dHz" % (audio_bytes / 2 / down_rate, down_rate))
            for k, v in got.items():
                if not v:
                    ok = False
            if audio_frames == 0:
                ok = False

    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


sys.exit(asyncio.run(main()))
