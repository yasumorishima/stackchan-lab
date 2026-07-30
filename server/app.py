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
import socket
import struct
import time
import uuid
import wave

import aiohttp
from aiohttp import web

import local_stt
import mcp_client
import opus_codec
import server_tools

log = logging.getLogger("stackchan")

# ---- 設定 -------------------------------------------------------------
PORT = int(os.environ.get("PORT", "8000"))
def _default_gateway() -> str:
    """既定経路のゲートウェイを経路表から読む（Linux）。

    ここを特定のルーターに決め打ちすると別のサブネットで動かなくなる。
    読めなければ空文字を返して呼び出し側の次の手に任せる。
    """
    try:
        with open("/proc/net/route", encoding="ascii") as f:
            next(f)                       # 見出し行
            for line in f:
                cols = line.split()
                # 宛先が 0.0.0.0 の行が既定経路。3 列目がゲートウェイ（little endian の hex）
                if len(cols) > 2 and cols[1] == "00000000":
                    return socket.inet_ntoa(struct.pack("<L", int(cols[2], 16)))
    except Exception:
        pass
    return ""


def _primary_ipv4() -> str:
    """本体から見えるこの機体の LAN アドレスを調べる。

    PUBLIC_HOST を明示しない運用のための保険。外に出る経路のソースアドレスを取るだけで、
    実際の通信は行わない（UDP connect はパケットを出さない）。
    既定経路のゲートウェイを先に試すのは、VPN 等が入っていても本体と同じ網の
    アドレスを選びたいため。
    """
    for target in (_default_gateway(), "8.8.8.8"):
        if not target:
            continue
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((target, 1))
            return s.getsockname()[0]
        except Exception:
            continue
        finally:
            s.close()
    return "127.0.0.1"


PUBLIC_HOST = os.environ.get("PUBLIC_HOST") or _primary_ipv4()
WS_TOKEN = os.environ.get("WS_TOKEN", "stackchan-local")

SAKURA_BASE = os.environ.get("SAKURA_BASE", "https://api.ai.sakura.ad.jp")
SAKURA_TOKEN = os.environ.get("SAKURA_TOKEN", "")
SAKURA_MODEL = os.environ.get("SAKURA_MODEL", "gpt-oss-120b")
# 本番（さくら）が使えない時に落ちる先。ネットが切れても・トークンが切れても
# 会話を続けるための保険。STT と TTS は既定でローカルなので、LLM だけ用意すれば
# 家の中だけで会話が成立する。空文字にすると無効
FALLBACK_LLM_BASE = os.environ.get("FALLBACK_LLM_BASE", "http://127.0.0.1:11434")
FALLBACK_LLM_MODEL = os.environ.get("FALLBACK_LLM_MODEL", "qwen2.5:3b")
FALLBACK_LLM_TOKEN = os.environ.get("FALLBACK_LLM_TOKEN", "dummy")
# 本番の待ち時間 [s]。落ちる先がある時は長く待たない（待つぶんだけ本体が黙る）。
# 1 発話でツール往復ぶん 2 回叩くので、60s だと最悪 2 分間無言になる
LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "60"))
# 30 秒は暫定。さくらの実応答時間を測れていないので、短くし過ぎると
# 「動いているのに遅いだけ」の本番を切ってローカルへ落としてしまう
PRIMARY_LLM_TIMEOUT = float(os.environ.get("PRIMARY_LLM_TIMEOUT", "30"))
# 本番が駄目だった後、しばらくは本番を試さずローカルへ直行する [s]。
# 落ちている間ずっと毎回 timeout ぶん待たされるのを防ぐ
PRIMARY_COOLDOWN = float(os.environ.get("PRIMARY_COOLDOWN", "120"))
_primary_down_until = 0.0
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
    "あなたは卓上ロボット「スタックちゃん」です。短く親しみやすく話します。"
    "天気や機体の操作は、推測せず必ずツールを使って答えます。"
    "前のやり取りに数値が出ていても、聞かれたらツールを呼び直します。"
    "発話の先頭の丸括弧はその発話の時刻です。",
)
# 読み上げる文の数と長さの上限。システム文で頼むのではなく code 側で切る
MAX_SENTENCES = int(os.environ.get("MAX_SENTENCES", "2"))
MAX_REPLY_CHARS = int(os.environ.get("MAX_REPLY_CHARS", "160"))
# 直前に調べた「いつの天気か」を Device-Id ごとに覚える時間 [s]。
# 省略形の追い質問（「じゃあ鳥取は？」）で引き継ぐためだが、長く持つと
# 何十分も前の「明日」を新しい質問に継いでしまうので会話の間だけにする
WHEN_TTL = float(os.environ.get("WHEN_TTL", "180"))
when_store = {}            # device_id -> (調べた時刻, when)


def remembered_when(device_id):
    """少し前に調べた「いつの天気か」。古ければ忘れる。"""
    hit = when_store.get(device_id)
    if not hit:
        return None
    if time.time() - hit[0] > WHEN_TTL:
        when_store.pop(device_id, None)
        return None
    return hit[1]


# 会話の続きを保つ時間 [s]。本体は一区切りごとに WebSocket を切るので、
# 接続をまたいで履歴を持たないと毎回はじめましての会話になる
HISTORY_TTL = float(os.environ.get("HISTORY_TTL", "1800"))
# 保持するメッセージ数。ツール往復も履歴に残すので、天気のような
# ツールを使う発話は 1 往復で 4 件（user / assistant+tool_calls / tool /
# assistant）消費する。以前と同じ体感の長さを保つため 10 から広げた。
HISTORY_TURNS = int(os.environ.get("HISTORY_TURNS", "20"))


def system_prompt() -> str:
    """システム文は固定にする（現在時刻をここに入れない）。

    tools はテンプレート上システム文の後ろに描画されるので、ここが発話ごとに
    変わると tools 約 250 トークンごとプロンプトキャッシュが捨てられる。
    RPi5 + qwen2.5:3b の実測で LLM 1 往復が 35s と 7.8s に分かれた
    （再利用トークン 96 対 360）。時刻は stamped_user() で発話側へ添える。
    """
    return SYSTEM_PROMPT


def stamped_user(text: str) -> dict:
    """ユーザー発話に、それを言われた時刻を添えた履歴エントリを作る。

    末尾へ足すだけなのでキャッシュ済みの接頭辞を壊さない。過去の発話も
    言われた時刻を保ったまま残るので「さっき」の解釈にも使える。
    """
    return {"role": "user",
            "content": "（" + server_tools.jst_stamp() + "）" + text}


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
class ChatHTTPError(RuntimeError):
    """chat completions が 200 以外を返した。status で切り分けるため型を持つ。"""

    def __init__(self, status, body):
        RuntimeError.__init__(self, "chat %d: %s" % (status, str(body)[:300]))
        self.status = status


# 「向こう側の都合」で失敗した時だけ落ちる。400/404/422 は自分の組み立てが
# 悪いので、ローカルへ投げ直しても同じように失敗する＝そのまま上げる
FALL_BACK_ON_STATUS = (401, 402, 403, 408, 425, 429, 500, 502, 503, 504)


def should_fall_back(e) -> bool:
    if isinstance(e, ChatHTTPError):
        return e.status in FALL_BACK_ON_STATUS
    # 接続不能・名前解決不能・timeout 等はネット側の問題なので落ちる
    return True


async def chat_once(session: aiohttp.ClientSession, history, tools,
                    base: str, model: str, token: str,
                    timeout: float = None) -> dict:
    """OpenAI 互換の chat completions を 1 回叩き、assistant メッセージを返す。

    tool_calls を落とさないよう、本文の文字列ではなく message 辞書を返す。
    """
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system_prompt()}] + history,
        "temperature": 0.7,
        "max_tokens": 200,
        "stream": False,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    headers = {"Authorization": "Bearer " + token,
               "Content-Type": "application/json", "Accept": "application/json"}
    async with session.post(base + "/v1/chat/completions", json=payload,
                            headers=headers,
                            timeout=aiohttp.ClientTimeout(
                                total=timeout or LLM_TIMEOUT)) as r:
        body = await r.json()
        if r.status != 200:
            raise ChatHTTPError(r.status, body)
        return body["choices"][0]["message"]


async def sakura_chat(session: aiohttp.ClientSession, history, tools=None) -> dict:
    """まず本番へ。向こうの都合で駄目ならローカルの小さいモデルへ落ちる。

    ネットが切れても・トークンが切れても黙り込まないようにするための保険。
    落ちた事実はログに残す（黙って別のモデルで答えると原因が追えない）。
    """
    global _primary_down_until
    usable = bool(FALLBACK_LLM_BASE) and FALLBACK_LLM_BASE != SAKURA_BASE
    if usable and time.time() < _primary_down_until:
        # 直前に駄目だったので本番は試さない（待ち時間ぶん黙るのを避ける）
        return await chat_once(session, history, tools, FALLBACK_LLM_BASE,
                               FALLBACK_LLM_MODEL, FALLBACK_LLM_TOKEN)
    try:
        msg = await chat_once(session, history, tools,
                              SAKURA_BASE, SAKURA_MODEL, SAKURA_TOKEN,
                              PRIMARY_LLM_TIMEOUT if usable else LLM_TIMEOUT)
        if _primary_down_until:
            log.info("本番 LLM (%s) が戻りました", SAKURA_MODEL)
            _primary_down_until = 0.0
        return msg
    except Exception as e:
        if not usable or not should_fall_back(e):
            raise
        _primary_down_until = time.time() + PRIMARY_COOLDOWN
        log.warning("本番 LLM (%s) が使えないのでローカル (%s) へ切り替えます"
                    "（%.0f 秒は本番を試しません）: %s",
                    SAKURA_MODEL, FALLBACK_LLM_MODEL, PRIMARY_COOLDOWN, e)
        msg = await chat_once(session, history, tools, FALLBACK_LLM_BASE,
                              FALLBACK_LLM_MODEL, FALLBACK_LLM_TOKEN)
        log.info("ローカル LLM (%s) で応答しました", FALLBACK_LLM_MODEL)
        return msg


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


TOOL_TAG_RE = re.compile(r"</?tool_call>|</?function_call>|</?tool_response>",
                         re.IGNORECASE)
TOOL_KEY_RE = re.compile(r"\"(?:name|arguments|parameters)\"\s*:")
# 発話へ添えた時刻（server_tools.jst_stamp の形）を、そのまま返答に書き写して
# くることがある。読み上げても邪魔なので落とす。jst_stamp の形だけを狙う
# （年・曜日・時刻はどれも欠けうるが、括弧の中が日時だけの場合に限る）
STAMP_ECHO_RE = re.compile(
    r"[（(]\s*(?:\d{4}\s*年\s*)?\d{1,2}\s*月\s*\d{1,2}\s*日\s*"
    r"(?:[（(][^（()）]{1,4}[）)]\s*)?"
    r"(?:\d{1,2}\s*時\s*\d{1,2}\s*分?\s*)?[）)]"
    r"|[（(]\s*\d{1,2}\s*時\s*\d{1,2}\s*分\s*[）)]")
FALLBACK_REPLY = "うまく答えられませんでした。もう一度お願いします。"
NEWLINES = chr(10) + chr(13)
# 行頭の箇条書き・見出し記号（読み上げると邪魔になる）
BULLET_RE = re.compile("(?m)^[ 　]*(?:[-*+#>]+[ 　]+|・[ 　]*|[0-9]+[.)][ 　]+)")


def _drop_tool_json(text: str) -> str:
    """釣り合いの取れた {...} のうち、ツール呼び出しらしいものを丸ごと落とす。

    入れ子があるので正規表現ではなく括弧を数えて切り出す。
    """
    out = []
    i = 0
    while i < len(text):
        if text[i] != "{":
            out.append(text[i])
            i += 1
            continue
        depth = 0
        j = i
        while j < len(text):
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if depth != 0:
            # 閉じていない = 生成が途中で切れた。以降は捨てる
            break
        block = text[i:j + 1]
        if not TOOL_KEY_RE.search(block):
            out.append(block)
        i = j + 1
    return "".join(out)


def clean_reply(text) -> str:
    """ツール呼び出しの断片を読み上げさせない。

    生成が max_tokens で切れるとツール呼び出しが壊れ、閉じタグや JSON の
    かけらが本文に残る（qwen2.5:3b の実測で本文が "</tool_call>" だけに
    なった）。そのまま合成すると意味のない音を読み上げるので落とす。
    どのモデルでも起きうるので、バックエンドに関係なく通す。
    """
    text = TOOL_TAG_RE.sub("", text or "")
    text = STAMP_ECHO_RE.sub("", text)
    # 箇条書きや見出しの記号は読み上げても意味が無い。改行も声には出ない
    text = BULLET_RE.sub("", text)
    text = re.sub("[" + NEWLINES + "]+", " ", text)
    text = _drop_tool_json(text)
    # 壊れた JSON の落ち穂（対応の取れない括弧）は読み上げても無意味
    text = re.sub(r"[{}\[\]]", "", text)
    return re.sub(r"[ 	]+", " ", text).strip()


def plain_sentences(text: str):
    """文の数を数えるための素直な分割（読み上げ用の塊 split_sentences とは別物）。

    split_sentences は 8 字未満の断片を前の文へくっつけるので、
    「晴れです。32度です。」が 1 塊になり文数として使えない。
    """
    return [s for s in re.split("(?<=[。．！？!?])", text or "") if s.strip()]


def shorten_reply(text: str) -> str:
    """読み上げる長さを code 側で切る。

    「2 文以内」をシステム文で頼むと、その指示ぶんだけツール選択が弱くなる
    （qwen2.5:3b 実測: 指示なし 呼び出し 7/9、指示ありの長いシステム文 1/9）。
    形は code で守る方が確実で、プロンプトも短く保てる。
    """
    parts = plain_sentences(text)
    if len(parts) > MAX_SENTENCES:
        text = "".join(parts[:MAX_SENTENCES])
    if len(text) > MAX_REPLY_CHARS:
        head = text[:MAX_REPLY_CHARS]
        cut = max(head.rfind("、"), head.rfind("。"))
        # 区切りが早すぎる位置なら、そこで切ると意味が残らないので字数で切る
        text = head[:cut + 1] if cut >= MAX_REPLY_CHARS // 2 else head + "。"
    return text.strip()


async def respond(session, history, tools=None, call_tool=None):
    """LLM に投げる。tool_calls が返ったら本体の MCP ツールを実行して結果を戻す。

    返り値は (最終文, 途中のツール往復)。ツール往復を履歴へ残さないと、
    次の省略形の追い質問（「じゃあ鳥取はどう？」）でモデルがツールを
    呼び直さない。qwen2.5:3b の実測では数値の作り話か「まだ調べてません」に
    なった。文脈にツールを使った跡が見えている必要がある。
    """
    if LLM_BACKEND != "sakura":
        return "ドライランです。音声の往復だけ確認しています。", []
    msgs = list(history)
    trace = []
    for _ in range(MAX_TOOL_ROUNDS):
        msg = await sakura_chat(session, msgs, tools)
        calls = msg.get("tool_calls") or []
        if not calls or call_tool is None:
            reply = shorten_reply(clean_reply(msg.get("content")))
            if not reply:
                log.warning("空の応答（本文=%r）",
                            (msg.get("content") or "")[:120])
                reply = FALLBACK_REPLY
            return reply, trace
        for c in calls:
            if not c.get("id"):
                # id を返さない実装（ローカルのモデル等）がある。空 id を履歴に
                # 残すと、別の API へ送り直した時に弾かれうるのでここで振る
                c["id"] = "call_" + uuid.uuid4().hex[:8]
        step = {"role": "assistant", "content": msg.get("content") or "",
                "tool_calls": calls}
        msgs.append(step)
        trace.append(step)
        for c in calls:
            fn = c.get("function") or {}
            name = fn.get("name") or ""
            args = {}
            broken = False
            try:
                args = json.loads(fn.get("arguments") or "{}")
                broken = not isinstance(args, dict)
            except Exception:
                broken = True
            if broken:
                # 既定の場所・既定の日で黙って答えると、聞かれていないことに
                # 答えることになる。読み直してもらう
                args = {}
                result = ("error: 引数が読み取れませんでした。"
                          "場所と日を入れて呼び直してください")
                log.warning("壊れた引数: %s(%r)", name,
                            (fn.get("arguments") or "")[:120])
            else:
                try:
                    result = await call_tool(name, args)
                except Exception as e:
                    result = "error: %s" % e
            log.info("tool %s(%s) -> %s", name, args, str(result)[:200])
            out = {"role": "tool", "tool_call_id": c["id"],
                   "content": str(result)}
            msgs.append(out)
            trace.append(out)
    return "うまく答えられませんでした。", trace


def last_user_text(history) -> str:
    """履歴の末尾にある発話の本文。添えた時刻の丸括弧は外す。

    when を発話の言葉から読む（server_tools.when_from_text）ので、
    stamped_user() が付けた時刻がそのまま混ざっていると邪魔になる。
    """
    for m in reversed(history):
        if m.get("role") == "user":
            return STAMP_ECHO_RE.sub("", str(m.get("content") or "")).strip()
    return ""


def trim_history(msgs):
    """直近 HISTORY_TURNS 件へ切り詰める。ただし tool を孤立させない。

    tool メッセージは tool_calls を持つ assistant の直後でなければ
    API 側で 400 になる。切った先頭がその途中なら user まで捨てる。
    """
    msgs = msgs[-HISTORY_TURNS:]
    while msgs and msgs[0].get("role") != "user":
        msgs.pop(0)
    return msgs


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
    hist_store = request.app["histories"]
    now = time.time()
    for stale in [k for k, v in hist_store.items() if now - v[0] > HISTORY_TTL]:
        hist_store.pop(stale, None)      # 期限切れを残さない
    kept = hist_store.get(device_id)
    history = []
    if kept and time.time() - kept[0] < HISTORY_TTL:
        history = kept[1]
        log.info("restored %d messages of history for %s", len(history), device_id)
    state = {"listening": False, "hello_done": False, "history": history,
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

    async def call_tool(name, args):
        """LLM の関数呼び出しを、サーバー側ツールと本体の MCP ツールへ振り分ける。

        本体側は接続の途中でツール一覧が揃うので、呼ばれた時点の state を見る。
        """
        if server_tools.has(name):
            ctx = {"utterance": last_user_text(state["history"]),
                   "last_when": remembered_when(device_id)}
            result = await server_tools.call(session, name, args, ctx)
            if ctx.get("resolved_when"):
                when_store[device_id] = (time.time(), ctx["resolved_when"])
            return result
        mcp = state["mcp"]
        if mcp is None:
            return "error: 本体のツールが使えません"
        return await mcp.call(name, args)

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
            state["history"] = trim_history(state["history"] + [stamped_user(text)])
            mcp = state["mcp"]
            task = state["mcp_task"]
            if mcp is not None and not mcp.tools and task is not None and not task.done():
                # 起動直後に話しかけられた場合、ツール一覧が揃うのを少しだけ待つ
                try:
                    await asyncio.wait_for(asyncio.shield(task), MCP_WAIT)
                except asyncio.TimeoutError:
                    log.warning("mcp handshake still pending, answering without tools")
                mcp = state["mcp"]
            # 同名のツールが 2 つ並ぶと API が弾く。振り分けはサーバー側を先に見るので、
            # 名前が衝突した本体側ツールはどのみち呼べない（現行ファームには無い）
            taken = {t["function"]["name"] for t in server_tools.specs()}
            device_tools = []
            for t in (mcp.openai_tools() if mcp else []):
                if t["function"]["name"] in taken:
                    log.warning("device tool %s is shadowed by a server tool",
                                t["function"]["name"])
                    continue
                device_tools.append(t)
            tools = server_tools.specs() + device_tools
            reply, trace = await respond(session, state["history"], tools=tools,
                                         call_tool=call_tool)
            state["history"].extend(trace)
            state["history"].append({"role": "assistant", "content": reply})
            state["history"] = trim_history(state["history"])
            hist_store[device_id] = (time.time(), list(state["history"]))
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
    app["histories"] = {}      # device_id -> (最終更新, messages)
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
