"""13 ツールの応答テキストを実際の読み上げ経路に通して確かめる。

LLM は挟まない（さくらの無料枠を使わない・ツール文の読みだけを見たい）。
見るのは 3 点: ①読み崩れしそうな字 ②shorten_reply で削られないか
③1 モーラあたりの秒（伸びの検知。0.16 秒/モーラを超えたら疑う）。
"""
import asyncio
import logging
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("TTS_BACKEND", "openjtalk")

import aiohttp                                    # noqa: E402
import app                                        # noqa: E402
import server_tools as T                          # noqa: E402

CASES = [
    ("get_weather", {"place": "横浜"}),
    ("get_usdjpy", {}),
    ("get_stock_index", {}),
    ("get_llm_quota", {}),
    ("get_crypto", {}),
    ("get_news", {}),
    ("get_quake", {}),
    ("get_warning", {}),
    ("get_typhoon", {}),
    ("get_heat", {}),
    ("get_train", {}),
    ("get_onthisday", {}),
    ("get_sky", {}),
]

# Open JTalk が読み崩しやすい字（英字の連なり・記号・全角英数）
RISKY = re.compile("[A-Za-z]{2,}|[%℃〜~/&±°Ａ-ｚ"
                   "０-９―─○●☆★]")
PACE_NG = 0.16          # 秒/モーラ。健全帯は実測 0.12〜0.13

PACES = []


class _Grab(logging.Handler):
    def emit(self, rec):
        m = re.match("読み ([0-9.]+) 秒/モーラ", rec.getMessage())
        if m:
            PACES.append(float(m.group(1)))


async def one(session, name, args):
    try:
        text = await T.call(session, name, args)
    except Exception as e:
        return {"name": name, "err": "%s: %s" % (type(e).__name__, str(e)[:80])}
    short = app.shorten_reply(text)
    segs = app.split_long_runs(short)
    PACES.clear()
    pcm = b""
    for seg in segs:
        seg_pcm, _ = await app._openjtalk_once(seg)
        pcm += seg_pcm
    sec = len(pcm) / 2.0 / app.DOWN_RATE
    return {"name": name, "text": text, "short": short, "segs": segs,
            "cut": short != text.strip(), "sec": sec,
            "paces": list(PACES), "risky": sorted(set(RISKY.findall(text)))}


async def main():
    app.log.addHandler(_Grab())
    app.log.setLevel(logging.INFO)
    ng = 0
    async with aiohttp.ClientSession() as session:
        for name, args in CASES:
            r = await one(session, name, args)
            if r.get("err"):
                print("[ERR ] %-16s %s" % (name, r["err"]))
                ng += 1
                continue
            worst = max(r["paces"]) if r["paces"] else 0.0
            flags = []
            if r["risky"]:
                flags.append("読み崩れ候補=" + ",".join(r["risky"]))
            if r["cut"]:
                flags.append("shorten で切れる")
            if worst > PACE_NG:
                flags.append("伸び %.3f 秒/モーラ" % worst)
            mark = "NG  " if flags else "ok  "
            ng += 1 if flags else 0
            print("[%s] %-16s %5.1f秒 %2d字 seg=%d 最悪%.3f  %s"
                  % (mark, name, r["sec"], len(r["text"]), len(r["segs"]),
                     worst, " / ".join(flags)))
            print("      本文: " + r["text"])
            if r["cut"]:
                print("      切後: " + r["short"])
    print("---- 要注意 %d / %d ----" % (ng, len(CASES)))


asyncio.run(main())
