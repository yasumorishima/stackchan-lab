"""「歌って」と声で頼んでから、実際に鳴る音までを通しで確かめる。

本体のふりをして WebSocket につなぎ、送られてきた音を復号して中身を測る。
実機で「歌えていない」と言われたので、**鳴った音そのもの**を見る:

  ①道具が 1 回だけ呼ばれるか
  ②歌う前に感想を言っていないか
  ③歌が届くか（長さ）
  ④歌が細切れでないか（無音の割合・声が続く長さ）
  ⑤楽譜どおりの旋律で歌えているか

  ./.venv/bin/python test_sing_e2e.py
"""
import asyncio
import io
import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import aiohttp

import app
import opus_codec

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))
UP_RATE, FRAME_MS = 16000, 60
DEVICE_ID = os.environ.get("DEVICE_ID", "aa:bb:cc:dd:ee:03")
LOG = os.environ.get("LOG", "/home/yasu/stackchan-server/server.log")
ASK = os.environ.get("ASK", "宮﨑の応援歌を歌って")
WHO = os.environ.get("WHO", "宮﨑")

ok = fail = 0


def check(name, cond, got=""):
    global ok, fail
    if cond:
        ok += 1
        print("OK %s %s" % (name, got))
    else:
        fail += 1
        print("NG %s %s" % (name, got))


def log_since(pos):
    with io.open(LOG, "rb") as f:
        f.seek(pos)
        return f.read().decode("utf-8", "replace")


async def speech(text):
    pcm = await app.openjtalk_tts(text)
    return app.resample_linear(pcm, app.DOWN_RATE, UP_RATE)


async def send_pcm(ws, enc, pcm):
    for p in enc.encode_stream(pcm):
        await ws.send_bytes(p)
        await asyncio.sleep(FRAME_MS / 1000.0)


def voiced_runs(pcm, rate):
    """声が続いた区間の長さ（秒）と、無音の割合を返す。"""
    x = np.frombuffer(pcm, dtype="<i2").astype("float64") / 32768.0
    step = int(rate * 0.02)
    n = len(x) // step * step
    if n < step:
        return [], 1.0
    frames = x[:n].reshape(-1, step)
    rms = np.sqrt((frames * frames).mean(axis=1))
    live = rms > max(0.02, 0.1 * rms.max())
    runs, cur = [], 0
    for v in live:
        if v:
            cur += 1
        elif cur:
            runs.append(cur * 0.02)
            cur = 0
    if cur:
        runs.append(cur * 0.02)
    return runs, 1.0 - float(live.mean())


async def main():
    ask = await speech(ASK)
    quiet = bytes(2 * int(UP_RATE * 1.5))
    log_from = os.path.getsize(LOG)
    headers = {"Protocol-Version": "1", "Device-Id": DEVICE_ID,
               "Client-Id": "sing-test"}
    said, frames_by_text, order = [], {}, []
    current = None

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
            print("「%s」と頼んだ" % ASK)

            # 歌は途中に割り込みの窓が入り、そのたびに tts stop が飛ぶ。
            # stop の数で打ち切ると歌が半分しか届かない（実測 13.5→7.9 秒）。
            # 音が来なくなってから畳む
            last = time.monotonic()
            end = time.monotonic() + 180
            while time.monotonic() < end:
                try:
                    msg = await asyncio.wait_for(ws.receive(), 4)
                except asyncio.TimeoutError:
                    # 歌の途中の窓では、本体の合図を待って数秒止まる
                    if frames_by_text and time.monotonic() - last > 8.0:
                        break
                    continue
                last = time.monotonic()
                if msg.type == aiohttp.WSMsgType.BINARY:
                    if current is not None:
                        frames_by_text.setdefault(current, []).append(msg.data)
                    continue
                if msg.type != aiohttp.WSMsgType.TEXT:
                    break
                d = json.loads(msg.data)
                if d.get("type") == "stt":
                    print("聞き取り: %s" % d.get("text"))
                    continue
                if d.get("type") != "tts":
                    continue
                if d.get("state") == "sentence_start":
                    current = d.get("text", "")
                    if current not in order:
                        order.append(current)
                        said.append(current)
                        print("鳴らし始め: %s" % current[:48])
                elif d.get("state") == "stop":
                    pass                # 窓のたびに来るので数えない

    tail = log_since(log_from)
    # 実際に用意した回数（呼ばれても 2 回目は作り直さない）。
    # ⚠️ 数えているのは cheer_song.prepare が出すログ。文言を変えたらここも変える
    # 音源から起こす経路と譜面から歌う経路で行が違う。どちらも 1 曲 1 行
    made = tail.count("モーラに対して音符") + tail.count("譜面から歌う")
    calls = tail.count("tool sing_cheer_song(")
    check("歌を用意するのは 1 回だけ", made == 1,
          "用意 %d 回 / 呼ばれ %d 回" % (made, calls))

    reply = said[0] if said else ""
    bad = [w for w in ("どうだった", "どうだ", "いかが", "聞いてみて",
                       "流れました", "流しました", "歌いました")
           if w in reply]
    check("歌う前に、まだ起きていないことを言わない", not bad,
          "返事: %s" % reply[:40])

    song_text = [t for t in order if "応援歌" in t and t != reply]
    check("歌が届いた", bool(song_text), "／".join(t[:20] for t in song_text))
    if not song_text:
        print("\n%d/%d" % (ok, ok + fail))
        return 1

    dec = opus_codec.Decoder(app.DOWN_RATE, 1)
    pcm = b"".join(dec.decode(p) for t in song_text
                   for p in frames_by_text.get(t, []))
    secs = len(pcm) / 2 / app.DOWN_RATE
    # サーバーが「何秒歌った」と書いた値と突き合わせる（途中で切れていないか）
    import re
    m = re.search(r"の応援歌を歌う（([0-9.]+) 秒", tail)
    made_secs = float(m.group(1)) if m else 0.0
    check("歌が最後まで届いた（送った長さの 95%% 以上）",
          made_secs > 0 and secs >= 0.95 * made_secs,
          "届いた %.1f 秒 / 送った %.1f 秒" % (secs, made_secs))

    runs, silence = voiced_runs(pcm, app.DOWN_RATE)
    med = float(np.median(runs)) if runs else 0.0
    check("細切れでない（無音 35%% 未満）", silence < 0.35,
          "無音 %.0f%%" % (silence * 100))
    check("声が続く長さ（中央値 0.12 秒以上）", med >= 0.12,
          "中央値 %.2f 秒・区間 %d 個" % (med, len(runs)))

    # 楽譜どおりか（起こした音符と、鳴った音の高さを突き合わせる）
    import cheer_song
    import sing_vv
    import test_transcribe as tt
    async with aiohttp.ClientSession() as s2:
        notes, tempo, name = await cheer_song.prepare(s2, WHO)
    # 歌う前に、声の出る帯へオクターブ単位で寄せている。楽譜と突き合わせる
    # ときも同じだけずらす（寄せた量を無視すると、合っていても外れて見える）
    shift = sing_vv.octave_shift([sing_vv.to_key(n[0]) for n in notes if n[0]])
    want = []
    for n in notes:
        if n[0] is None:
            continue
        v = sing_vv.to_key(n[0]) + shift
        if not want or want[-1] != v:
            want.append(v)
    x = np.frombuffer(pcm, dtype="<i2").astype("float64") / 32768.0
    got = tt.contour(x, app.DOWN_RATE)
    cost = tt.align_cost(got, want) if got and want else 99.0
    check("楽譜どおりの旋律で歌えている（平均 1.5 半音以内）", cost <= 1.5,
          "平均 %.2f 半音（楽譜 %d 音 / 鳴った %d 音・帯へ %+d 半音）"
          % (cost, len(want), len(got), shift))

    print("\n%d/%d" % (ok, ok + fail))
    return 1 if fail else 0


sys.exit(asyncio.run(main()))
