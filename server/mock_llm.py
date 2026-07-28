"""さくらの AI Engine の代わりに立てるモック（OpenAI 互換）。

トークンが無い状態でも、サーバー側の実装（chat completions のリクエスト形・
tool_calls の解釈・音声文字起こしの応答処理）が正しいかを検証するために使う。
本物のエンドポイントと同じパスを同じ形で返すので、切り替えは SAKURA_BASE だけ。

ツールの選び方だけは本物の LLM の代わりに素朴な語判定で決める。狙いは
「サーバーが tools を正しく渡し、返した tool_calls を実行して結果を戻せるか」の検証で、
どのツールを選ぶかの賢さではない。

  ./.venv/bin/python mock_llm.py            # 127.0.0.1:8100 で待つ
"""
import json
import logging
import os

from aiohttp import web

from places import PLACES

# 本物の LLM なら発話から地名を拾える。モックはツール表と同じ語で代用する
PLACE_WORDS = sorted((p for p in PLACES if not p.isascii()), key=len, reverse=True)

log = logging.getLogger("mock_llm")
PORT = int(os.environ.get("MOCK_PORT", "8100"))
seen = {"chat": 0, "stt": 0, "tools_offered": 0, "tool_result_seen": 0, "tts": 0,
        "called": []}
VOICEVOX = os.environ.get("VOICEVOX_URL", "http://127.0.0.1:50021")


def pick_tool(text, tools):
    """発話からツールを選ぶ。名前で探すので、渡ってきていなければ選ばない。"""
    names = [t.get("function", {}).get("name", "") for t in tools]
    if "天気" in text and "get_weather" in names:
        when = "tomorrow" if any(w in text for w in ("明日", "あした", "あす")) else "today"
        place = None
        for p in PLACE_WORDS:          # 長いものから当てる（「東京」より「東京都」）
            if p in text:
                place = p
                break
        args = {"when": when}
        if place:
            args["place"] = place
        return "get_weather", args
    if "音量" in text:
        for n in names:
            if "volume" in n:
                return n, {"volume": 50}
    return None, None


async def chat(request):
    body = await request.json()
    seen["chat"] += 1
    msgs = body.get("messages") or []
    tools = body.get("tools") or []
    if tools:
        seen["tools_offered"] = len(tools)
    auth = request.headers.get("Authorization", "")
    system = next((m.get("content", "") for m in msgs if m.get("role") == "system"), "")
    seen["system_has_time"] = "現在は" in system
    log.info("chat #%d messages=%d tools=%d auth=%s",
             seen["chat"], len(msgs), len(tools),
             "yes" if auth.startswith("Bearer ") else "NO")

    # 直前が tool の結果なら、それを読んだ体で最終回答を返す
    if msgs and msgs[-1].get("role") == "tool":
        seen["tool_result_seen"] += 1
        result = msgs[-1].get("content") or ""
        log.info("tool result: %s", result)
        return web.json_response({"choices": [{"message": {
            "role": "assistant",
            "content": "調べました。%s ということです。" % result}}]})

    user = next((m.get("content", "") for m in reversed(msgs)
                 if m.get("role") == "user"), "")
    name, args = pick_tool(user, tools)
    if name:
        seen["called"].append(name)
        log.info("-> tool_call %s %s", name, args)
        return web.json_response({"choices": [{"message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_%d" % seen["chat"], "type": "function",
                            "function": {"name": name,
                                         "arguments": json.dumps(args)}}],
        }}]})

    return web.json_response({"choices": [{"message": {
        "role": "assistant", "content": "モックの応答です。ちゃんと届いています。"}}]})


async def transcriptions(request):
    seen["stt"] += 1
    data = await request.post()
    f = data.get("file")
    size = len(f.file.read()) if f is not None else 0
    log.info("stt #%d wav=%d bytes", seen["stt"], size)
    return web.json_response({"text": "モックの文字起こしです"})


async def tts_proxy(request):
    """さくら側の VOICEVOX 形式エンドポイント（/tts/v1/...）の代役。

    実体はローカルの VOICEVOX にそのまま中継する。認証ヘッダの有無だけ記録して、
    サーバー側の「さくらの TTS を叩く経路」を本物と同じ形で通せるようにする。
    """
    import aiohttp
    seen["tts"] = seen.get("tts", 0) + 1
    seen["tts_auth"] = request.headers.get("Authorization", "").startswith("Bearer ")
    upstream = VOICEVOX + "/" + request.match_info["tail"]
    body = await request.read()
    headers = {"Content-Type": request.headers.get("Content-Type", "application/json")}
    async with aiohttp.ClientSession() as s:
        async with s.post(upstream, params=request.query, data=body, headers=headers,
                          timeout=aiohttp.ClientTimeout(total=120)) as r:
            payload = await r.read()
            return web.Response(status=r.status, body=payload,
                                content_type=r.content_type)


async def stats(request):
    return web.json_response(seen)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s mock %(message)s")
    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat)
    app.router.add_post("/v1/audio/transcriptions", transcriptions)
    app.router.add_post("/tts/v1/{tail:.*}", tts_proxy)
    app.router.add_get("/stats", stats)
    web.run_app(app, host="127.0.0.1", port=PORT, access_log=None)


if __name__ == "__main__":
    main()
