"""読み上げ中に話しかけたら残りをやめるか（barge-in）を、実機ぬきで確かめる。

本体のふるまいのうち、割り込みの成否を決める 2 点を真似る:
  - TTS を受けている間はマイクを送らない（実機の実測は 0〜2 フレーム）
  - tts stop を受けてから listen start を送るまで遅れる
    （実機のログで 0.5〜1.1 秒。ここが肝で、サーバーが貯まりの見積りだけで
     窓を開けると窓が閉じたあとに本体が聞き始めて空振りする）

  ./.venv/bin/python test_barge.py
"""
import asyncio
import io
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
DEVICE_ID = os.environ.get("DEVICE_ID", "aa:bb:cc:dd:ee:02")
BODY_DELAY = float(os.environ.get("BODY_DELAY", "0.7"))
LOG = os.environ.get("LOG", "/home/yasu/stackchan-server/server.log")
# 長い返事になる問いを選ぶ（窓は 6 秒ぶん送ってから開くので短い返事では開かない）。
# ⚠️ 2026-08-06 に「燃油サーチャージを教えて」で FAIL した。道具は 6 方面すべてを
# 返したのに、モデルが読まずに「どこの燃油サーチャージが知りたいか」と聞き返し、
# 返事が 4.6 秒＝窓の条件（6 秒）に届かなかった＝**割り込みの機構ではなく、
# 問いの選び方の失敗**。応援歌の歌詞は VERBATIM_TOOLS でそのまま読ませるので
# 長さが決まる（実測 11.9 秒）
ASK = os.environ.get("ASK", "宮﨑の応援歌を教えて")
BARGE = os.environ.get("BARGE", "ねえ、ちょっと待って")
ABORT_LINE = "読み上げ中に話しかけられたので残りをやめる"


async def speech(text):
    """本体が送ってくるマイク音声の代わり（Open JTalk で作る）。"""
    pcm = await app.openjtalk_tts(text)
    return app.resample_linear(pcm, app.DOWN_RATE, UP_RATE)


def log_since(pos):
    with io.open(LOG, "rb") as f:
        f.seek(pos)
        return f.read().decode("utf-8", "replace")


async def send_pcm(ws, enc, pcm):
    """実時間で流す。速く流すと窓の中で発話が終わってしまい試験にならない。"""
    for p in enc.encode_stream(pcm):
        await ws.send_bytes(p)
        await asyncio.sleep(FRAME_MS / 1000.0)


async def main() -> int:
    print("音声を用意する…")
    ask = await speech(ASK)
    barge = await speech(BARGE)
    quiet = bytes(2 * int(UP_RATE * 1.5))
    print("問い %.2fs（%s）／割り込み %.2fs（%s）"
          % (len(ask) / 2 / UP_RATE, ASK, len(barge) / 2 / UP_RATE, BARGE))

    log_from = os.path.getsize(LOG)
    headers = {"Protocol-Version": "1", "Device-Id": DEVICE_ID,
               "Client-Id": "barge-test"}
    st = {"first": None, "after_barge": [], "new": None,
          "barged_at": None, "frames": 0}

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
            await send_pcm(ws, enc, ask + quiet)
            print("問いを送り終えた（listen stop は送らない＝実機と同じ）")

            async def barge_in():
                """tts stop を受けた本体のふるまい。遅れて聞き始め、話す。"""
                await asyncio.sleep(BODY_DELAY)
                st["barged_at"] = time.monotonic()
                await ws.send_str(json.dumps({
                    "type": "listen", "state": "start", "mode": "auto"}))
                print("%.1f 秒遅れて聞き始め、割り込んで話す" % BODY_DELAY)
                await send_pcm(ws, enc, barge + quiet)

            task = None
            end = time.monotonic() + 150
            while time.monotonic() < end:
                try:
                    msg = await asyncio.wait_for(ws.receive(), 20)
                except asyncio.TimeoutError:
                    break
                if msg.type == aiohttp.WSMsgType.BINARY:
                    st["frames"] += 1
                    continue
                if msg.type != aiohttp.WSMsgType.TEXT:
                    break
                d = json.loads(msg.data)
                if d.get("type") == "mcp":
                    continue
                if d.get("type") == "stt":
                    print("STT: %s" % d.get("text"))
                    continue
                if d.get("type") != "tts":
                    continue
                if d.get("state") == "sentence_start":
                    text = d.get("text", "")
                    if st["first"] is None:
                        st["first"] = text
                        print("読み上げ開始: %s" % text[:40])
                    elif st["barged_at"] and text == st["first"]:
                        st["after_barge"].append(text)
                    elif st["barged_at"] and text != st["first"]:
                        st["new"] = text
                        print("割り込みへの返事: %s" % text[:40])
                        break
                elif d.get("state") == "stop":
                    if st["first"] and task is None:
                        # ここが割り込みの窓（実機も窓か終わりかを区別しない）
                        print("tts stop を受けた（frames=%d）" % st["frames"])
                        task = asyncio.ensure_future(barge_in())
            if task is not None and not task.done():
                task.cancel()

    tail = log_since(log_from)
    # 窓が開く前提（返事が 6 秒以上）を満たしたかを先に見る。満たしていない
    # のに FAIL と言うと、機構が壊れたのか問いが短かっただけなのか読めない
    spoke = [ln for ln in tail.splitlines() if "spoke " in ln]
    secs = 0.0
    for ln in spoke:
        try:
            secs = max(secs, float(ln.split("(")[1].split("s)")[0]))
        except (IndexError, ValueError):
            pass
    if secs and secs < 6.0:
        print("前提を満たしていない: 返事が %.1f 秒しかなく窓が開かない"
              "（6 秒以上の返事になる問いを ASK= で渡す）" % secs)
    aborted = ABORT_LINE in tail
    for line in tail.splitlines():
        if "文の切れ目で" in line or ABORT_LINE in line:
            print(line[11:])

    ok = True
    if st["first"] is None:
        print("NG 返事が始まらなかった")
        ok = False
    if st["barged_at"] is None:
        print("NG 割り込みの機会（tts stop）が来なかった")
        ok = False
    if st["after_barge"]:
        print("NG 割り込んだのに元の返事が %d 文続いた" % len(st["after_barge"]))
        ok = False
    if not aborted:
        print("NG サーバーが %r を出していない" % ABORT_LINE)
        ok = False
    if st["new"] is None:
        print("NG 割り込みへの返事が来なかった")
        ok = False
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.get_event_loop().run_until_complete(main()))
