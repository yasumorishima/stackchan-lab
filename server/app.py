"""スタックちゃん（XiaoZhi プロトコル）用の自前サーバー。

役割は 2 つ。
  1. OTA エンドポイント: 本体が起動時に叩く。更新を止めつつ WebSocket の接続先を返す
  2. WebSocket サーバー: 音声対話の本体（Opus 受信 -> STT -> LLM -> TTS -> Opus 送信）

本体側のファームウェアは公式バイナリのまま。NVS の wifi/ota_url をこのサーバーに
向けるだけで接続先が変わる（アバター・MCP ツール・アプリ連携は維持される）。
"""
import asyncio
import io
import json
import logging
import math
import os
import re
import struct
import time
import uuid
import wave

import aiohttp
from aiohttp import web

import local_stt
import mcp_client
import opus_codec

log = logging.getLogger("stackchan")

# ---- 設定 -------------------------------------------------------------
PORT = int(os.environ.get("PORT", "8000"))
def _primary_ipv4() -> str:
    """本体から見えるこの機体の LAN アドレスを調べる。

    PUBLIC_HOST を明示しない運用のための保険。外に出る経路のソースアドレスを取るだけで、
    実際の通信は行わない（UDP connect はパケットを出さない）。
    """
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.168.10.1", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


PUBLIC_HOST = os.environ.get("PUBLIC_HOST") or _primary_ipv4()
WS_TOKEN = os.environ.get("WS_TOKEN", "stackchan-local")

SAKURA_BASE = os.environ.get("SAKURA_BASE", "https://api.ai.sakura.ad.jp")
SAKURA_TOKEN = os.environ.get("SAKURA_TOKEN", "")
SAKURA_MODEL = os.environ.get("SAKURA_MODEL", "gpt-oss-120b")
SAKURA_STT_MODEL = os.environ.get("SAKURA_STT_MODEL", "")

# stt: sherpa / vosk / whisper / sakura / dry   tts: voicevox / sakura / tone   llm: sakura / dry
LOCAL_STT = ("sherpa", "vosk", "whisper")
MAX_TOOL_ROUNDS = int(os.environ.get("MAX_TOOL_ROUNDS", "3"))
MCP_WAIT = float(os.environ.get("MCP_WAIT", "3.0"))   # ツール一覧を待つ上限 [s]
STT_BACKEND = os.environ.get("STT_BACKEND", "dry")
TTS_BACKEND = os.environ.get("TTS_BACKEND", "tone")
LLM_BACKEND = os.environ.get("LLM_BACKEND", "dry")

VOICEVOX_URL = os.environ.get("VOICEVOX_URL", "http://127.0.0.1:50021")
VOICEVOX_SPEAKER = int(os.environ.get("VOICEVOX_SPEAKER", "3"))  # 3 = ずんだもん ノーマル

UP_RATE = 16000     # 本体 -> サーバー（ファーム固定）
DOWN_RATE = int(os.environ.get("DOWN_RATE", "24000"))
FRAME_MS = 60

SYSTEM_PROMPT = os.environ.get(
    "SYSTEM_PROMPT",
    "あなたは卓上ロボット「スタックちゃん」です。親しみやすく、短く話します。"
    "返答は 2 文以内、読み上げるので記号や箇条書きは使いません。",
)


# ---- 音声ユーティリティ ------------------------------------------------
def pcm_to_wav(pcm: bytes, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def wav_to_pcm(data: bytes):
    with wave.open(io.BytesIO(data), "rb") as w:
        return w.readframes(w.getnframes()), w.getframerate(), w.getnchannels(), w.getsampwidth()


def resample_linear(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """16bit mono の線形補間リサンプル。audioop 非依存（3.13 で削除されるため）。"""
    if src_rate == dst_rate:
        return pcm
    n = len(pcm) // 2
    if n == 0:
        return b""
    src = struct.unpack("<%dh" % n, pcm)
    m = max(1, int(n * dst_rate / src_rate))
    out = []
    ratio = (n - 1) / (m - 1) if m > 1 else 0.0
    for i in range(m):
        pos = i * ratio
        j = int(pos)
        frac = pos - j
        a = src[j]
        b = src[j + 1] if j + 1 < n else a
        out.append(int(a + (b - a) * frac))
    return struct.pack("<%dh" % m, *out)


def tone_pcm(rate: int, seconds=0.6, freq=660.0) -> bytes:
    n = int(rate * seconds)
    vals = []
    for i in range(n):
        env = min(1.0, i / (rate * 0.02), (n - i) / (rate * 0.05))
        vals.append(int(8000 * env * math.sin(2 * math.pi * freq * i / rate)))
    return struct.pack("<%dh" % n, *vals)


# ---- 外部サービス ------------------------------------------------------
async def sakura_chat(session: aiohttp.ClientSession, history, tools=None) -> dict:
    """OpenAI 互換の chat completions を叩き、assistant メッセージをそのまま返す。

    tool_calls を落とさないよう、本文の文字列ではなく message 辞書を返す。
    """
    url = SAKURA_BASE + "/v1/chat/completions"
    payload = {
        "model": SAKURA_MODEL,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + history,
        "temperature": 0.7,
        "max_tokens": 200,
        "stream": False,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    headers = {"Authorization": "Bearer " + SAKURA_TOKEN,
               "Content-Type": "application/json", "Accept": "application/json"}
    async with session.post(url, json=payload, headers=headers,
                            timeout=aiohttp.ClientTimeout(total=60)) as r:
        body = await r.json()
        if r.status != 200:
            raise RuntimeError("sakura chat %d: %s" % (r.status, body))
        return body["choices"][0]["message"]


async def sakura_stt(session: aiohttp.ClientSession, pcm: bytes) -> str:
    url = SAKURA_BASE + "/v1/audio/transcriptions"
    form = aiohttp.FormData()
    form.add_field("file", pcm_to_wav(pcm, UP_RATE),
                   filename="a.wav", content_type="audio/wav")
    if SAKURA_STT_MODEL:
        form.add_field("model", SAKURA_STT_MODEL)
    headers = {"Authorization": "Bearer " + SAKURA_TOKEN}
    async with session.post(url, data=form, headers=headers,
                            timeout=aiohttp.ClientTimeout(total=90)) as r:
        body = await r.json()
        if r.status != 200:
            raise RuntimeError("sakura stt %d: %s" % (r.status, body))
        return (body.get("text") or "").strip()


async def voicevox_tts(session: aiohttp.ClientSession, text: str, base: str, headers=None):
    headers = headers or {}
    async with session.post(base + "/audio_query", params={"text": text, "speaker": VOICEVOX_SPEAKER},
                            headers=headers, timeout=aiohttp.ClientTimeout(total=60)) as r:
        if r.status != 200:
            raise RuntimeError("voicevox audio_query %d: %s" % (r.status, await r.text()))
        query = await r.json()
    async with session.post(base + "/synthesis", params={"speaker": VOICEVOX_SPEAKER},
                            json=query, headers=headers,
                            timeout=aiohttp.ClientTimeout(total=120)) as r:
        if r.status != 200:
            raise RuntimeError("voicevox synthesis %d: %s" % (r.status, await r.text()))
        wav = await r.read()
    pcm, rate, ch, width = wav_to_pcm(wav)
    if ch != 1 or width != 2:
        raise RuntimeError("unexpected wav: ch=%d width=%d" % (ch, width))
    return resample_linear(pcm, rate, DOWN_RATE)


async def synthesize(session, text: str) -> bytes:
    if TTS_BACKEND == "voicevox":
        return await voicevox_tts(session, text, VOICEVOX_URL)
    if TTS_BACKEND == "sakura":
        return await voicevox_tts(session, text, SAKURA_BASE + "/tts/v1",
                                  {"Authorization": "Bearer " + SAKURA_TOKEN})
    return tone_pcm(DOWN_RATE)


def split_sentences(text: str):
    """読み上げ用に文へ割る。短い断片は前の文にくっつける。"""
    parts = [s.strip() for s in re.split("(?<=[。．！？!?])", text) if s.strip()]
    out = []
    for s in parts:
        if out and len(out[-1]) < 8:
            out[-1] = out[-1] + s
        else:
            out.append(s)
    return out or [text.strip() or "..."]


async def transcribe(session, pcm: bytes) -> str:
    if STT_BACKEND in LOCAL_STT:
        t0 = time.monotonic()
        text = await local_stt.transcribe(STT_BACKEND, pcm, UP_RATE)
        secs = len(pcm) / 2 / UP_RATE
        took = time.monotonic() - t0
        log.info("stt(%s) %.2fs audio in %.2fs (rtf %.2f): %s",
                 STT_BACKEND, secs, took, took / max(secs, 1e-6), text)
        return text
    if STT_BACKEND == "sakura":
        return await sakura_stt(session, pcm)
    return "（ドライラン: 音声 %.1f 秒を受信しました）" % (len(pcm) / 2 / UP_RATE)


async def respond(session, history, tools=None, call_tool=None) -> str:
    """LLM に投げる。tool_calls が返ったら本体の MCP ツールを実行して結果を戻す。"""
    if LLM_BACKEND != "sakura":
        return "ドライランです。音声の往復だけ確認しています。"
    msgs = list(history)
    for _ in range(MAX_TOOL_ROUNDS):
        msg = await sakura_chat(session, msgs, tools)
        calls = msg.get("tool_calls") or []
        if not calls or call_tool is None:
            return (msg.get("content") or "").strip()
        msgs.append({"role": "assistant", "content": msg.get("content") or "",
                     "tool_calls": calls})
        for c in calls:
            fn = c.get("function") or {}
            name = fn.get("name") or ""
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except Exception:
                args = {}
            try:
                result = await call_tool(name, args)
            except Exception as e:
                result = "error: %s" % e
            log.info("tool %s(%s) -> %s", name, args, str(result)[:200])
            msgs.append({"role": "tool", "tool_call_id": c.get("id") or "",
                         "content": str(result)})
    return "うまく答えられませんでした。"


# ---- OTA ---------------------------------------------------------------
async def ota_handler(request: web.Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    current = (body.get("application") or {}).get("version") or "0.0.0"
    device_id = request.headers.get("Device-Id", "?")
    log.info("OTA request from %s (version %s)", device_id, current)
    return web.json_response({
        # 現在版と同じ version を返して OTA を走らせない
        "firmware": {"version": current, "url": ""},
        "websocket": {
            "url": "ws://%s:%d/ws" % (PUBLIC_HOST, PORT),
            "token": WS_TOKEN,
            "version": 1,
        },
        "server_time": {
            "timestamp": int(time.time() * 1000),
            "timezone_offset": 540,
        },
    })


# ---- WebSocket ---------------------------------------------------------
async def ws_handler(request: web.Request):
    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=0)
    await ws.prepare(request)
    session: aiohttp.ClientSession = request.app["http"]

    device_id = request.headers.get("Device-Id", "?")
    session_id = uuid.uuid4().hex[:16]
    log.info("WS connected device=%s session=%s", device_id, session_id)

    decoder = opus_codec.Decoder(UP_RATE, 1)
    encoder = opus_codec.Encoder(DOWN_RATE, 1, FRAME_MS)
    pcm_chunks = []
    state = {"listening": False, "hello_done": False, "history": [],
             "stream": None, "mcp": None, "mcp_task": None, "task": None}

    async def send_json(obj):
        await ws.send_str(json.dumps(obj, ensure_ascii=False))

    async def speak(text: str):
        # 文ごとに合成して送る。次の文は今の文を流している裏で作る（初音までを短く）
        sentences = split_sentences(text)
        t0 = time.monotonic()
        await send_json({"type": "tts", "state": "start"})
        nxt = asyncio.create_task(synthesize(session, sentences[0]))
        sent = 0
        first = None
        for i, s in enumerate(sentences):
            pcm = await nxt
            if i + 1 < len(sentences):
                nxt = asyncio.create_task(synthesize(session, sentences[i + 1]))
            await send_json({"type": "tts", "state": "sentence_start", "text": s})
            for packet in encoder.encode_stream(pcm):
                await ws.send_bytes(packet)
                sent += 1
                if first is None:
                    first = time.monotonic() - t0
                await asyncio.sleep(FRAME_MS / 1000 * 0.85)
        await send_json({"type": "tts", "state": "stop"})
        log.info("spoke %d frames (%.1fs) in %d sentences, first audio %.2fs: %s",
                 sent, sent * FRAME_MS / 1000, len(sentences), first or -1, text)

    async def handle_utterance(pcm, stream):
        if len(pcm) < UP_RATE * 2 * 0.3:
            log.info("utterance too short (%d bytes), ignored", len(pcm))
            return
        try:
            if stream is not None:
                t0 = time.monotonic()
                text = await stream.final()
                log.info("stt(vosk streaming) %.2fs audio, tail %.2fs: %s",
                         len(pcm) / 2 / UP_RATE, time.monotonic() - t0, text)
            else:
                text = await transcribe(session, pcm)
            log.info("STT: %s", text)
            await send_json({"type": "stt", "text": text})
            state["history"] = (state["history"] + [{"role": "user", "content": text}])[-10:]
            mcp = state["mcp"]
            task = state["mcp_task"]
            if mcp is not None and not mcp.tools and task is not None and not task.done():
                # 起動直後に話しかけられた場合、ツール一覧が揃うのを少しだけ待つ
                try:
                    await asyncio.wait_for(asyncio.shield(task), MCP_WAIT)
                except asyncio.TimeoutError:
                    log.warning("mcp handshake still pending, answering without tools")
                mcp = state["mcp"]
            reply = await respond(session, state["history"],
                                  tools=mcp.openai_tools() if mcp else None,
                                  call_tool=mcp.call if mcp else None)
            state["history"].append({"role": "assistant", "content": reply})
            log.info("LLM: %s", reply)
            await send_json({"type": "llm", "emotion": "happy", "text": "😀"})
            await speak(reply)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("utterance failed")
            await send_json({"type": "tts", "state": "stop"})
            await send_json({"type": "stt", "text": "エラー: %s" % e})

    async for msg in ws:
        if msg.type == aiohttp.WSMsgType.BINARY:
            if state["listening"]:
                try:
                    chunk = decoder.decode(msg.data)
                    pcm_chunks.append(chunk)
                    if state["stream"] is not None:
                        await state["stream"].feed(chunk)
                except Exception:
                    log.exception("opus decode failed (%d bytes)", len(msg.data))
        elif msg.type == aiohttp.WSMsgType.TEXT:
            try:
                data = json.loads(msg.data)
            except Exception:
                log.warning("non-json text frame: %r", msg.data[:200])
                continue
            mtype = data.get("type")
            log.info("<- %s", msg.data[:300])
            if mtype == "hello":
                await send_json({
                    "type": "hello",
                    "transport": "websocket",   # この値は本体が厳密に比較する
                    "session_id": session_id,
                    "audio_params": {"sample_rate": DOWN_RATE,
                                     "frame_duration": FRAME_MS},
                })
                state["hello_done"] = True
                if (data.get("features") or {}).get("mcp"):
                    mcp = mcp_client.McpSession(send_json)
                    state["mcp"] = mcp

                    async def _handshake():
                        try:
                            await mcp.handshake()
                        except Exception:
                            log.exception("mcp handshake failed")
                            state["mcp"] = None

                    state["mcp_task"] = asyncio.create_task(_handshake())
            elif mtype == "listen":
                ls = data.get("state")
                if ls in ("start", "detect"):
                    state["listening"] = True
                    pcm_chunks.clear()
                    state["stream"] = (local_stt.VoskStream(UP_RATE)
                                       if STT_BACKEND == "vosk" else None)
                elif ls == "stop":
                    state["listening"] = False
                    pcm = b"".join(pcm_chunks)
                    pcm_chunks.clear()
                    stream = state["stream"]
                    state["stream"] = None
                    prev = state["task"]
                    if prev is not None and not prev.done():
                        log.info("前の発話を処理中なので中断する")
                        prev.cancel()
                    # 受信ループの中で待つと、本体からの MCP 応答を読めなくなる
                    state["task"] = asyncio.create_task(handle_utterance(pcm, stream))
            elif mtype == "abort":
                state["listening"] = False
                pcm_chunks.clear()
                state["stream"] = None
                if state["task"] is not None and not state["task"].done():
                    state["task"].cancel()
            elif mtype == "mcp":
                if state["mcp"] is not None:
                    state["mcp"].handle(data.get("payload") or {})
                else:
                    log.info("mcp message before handshake: %s", msg.data[:200])
        elif msg.type == aiohttp.WSMsgType.ERROR:
            log.error("ws error: %s", ws.exception())

    decoder.close()
    encoder.close()
    for t in (state["task"], state["mcp_task"]):
        if t is not None and not t.done():
            t.cancel()
    log.info("WS closed device=%s session=%s (hello_done=%s)",
             device_id, session_id, state["hello_done"])
    return ws


async def health(request):
    return web.json_response({"ok": True, "stt": STT_BACKEND,
                              "llm": LLM_BACKEND, "tts": TTS_BACKEND})


async def on_startup(app):
    app["http"] = aiohttp.ClientSession()
    if STT_BACKEND in LOCAL_STT:
        # 初回発話でモデル読み込みを待たせない
        await asyncio.to_thread(local_stt.warmup, STT_BACKEND)
        log.info("local stt warmed up: %s", STT_BACKEND)


async def on_cleanup(app):
    await app["http"].close()


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    app = web.Application()
    app.router.add_route("*", "/xiaozhi/ota/", ota_handler)
    app.router.add_route("*", "/xiaozhi/ota", ota_handler)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/health", health)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    log.info("listening on 0.0.0.0:%d  ota=http://%s:%d/xiaozhi/ota/  ws=ws://%s:%d/ws",
             PORT, PUBLIC_HOST, PORT, PUBLIC_HOST, PORT)
    log.info("backends: stt=%s llm=%s tts=%s", STT_BACKEND, LLM_BACKEND, TTS_BACKEND)
    web.run_app(app, host="0.0.0.0", port=PORT, access_log=None)


if __name__ == "__main__":
    main()
