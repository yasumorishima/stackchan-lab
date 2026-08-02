"""伸びている断片を特定する（split_long_runs の結果を 1 片ずつ測る）。"""
import asyncio
import logging
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("TTS_BACKEND", "openjtalk")
import app                                        # noqa: E402

TEXTS = [
    "今月このサーバーから使った LLM リクエストは4回で、無料枠3000回の残りは"
    "およそ2996回です。サーバーの外で使った分（検証など）は数えられないので、"
    "正確な値はコントロールパネルの利用量が正です。",
    "主なニュースは、【地震速報】熊本 宇城市で震度５弱 津波の心配なし、"
    "熊本地震 関連調査中含め36人死亡 住宅被害3400超 県発表、岡山 矢掛町 "
    "2歳の男の子が行方不明 祖母の家に帰省中、以上です。",
]

PACES = []


class _Grab(logging.Handler):
    def emit(self, rec):
        m = re.match("読み ([0-9.]+) 秒/モーラ", rec.getMessage())
        if m:
            PACES.append(float(m.group(1)))


async def main():
    app.log.addHandler(_Grab())
    app.log.setLevel(logging.INFO)
    for text in TEXTS:
        print("=== " + text[:20])
        for seg in app.split_long_runs(text):
            PACES.clear()
            pcm, pace = await app._openjtalk_once(seg)
            sec = len(pcm) / 2.0 / app.DOWN_RATE
            pace = pace or 0.0
            mark = "NG" if pace > 0.16 else "ok"
            print("  [%s] %2d字 %5.2f秒 %.3f秒/モーラ  %s"
                  % (mark, len(seg), sec, pace, seg))

asyncio.run(main())
