"""何が伸びを決めるのかを実測する（字数 / モーラ / アクセント句長）。

Open JTalk の全文脈ラベルの F: 欄は「今のアクセント句のモーラ数」。
"""
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app                                        # noqa: E402

CASES = [
    "無料枠3000回の残りはおよそ2996回です",
    "残りはおよそ2996回です",
    "およそ2996回です",
    "2996回です",
    "無料枠3000回の",
    "4回で",
    "サーバーの外で使った分（検証など）は",
    "今月このサーバーから使った LLM リクエストは",
    "数えられないので、正確な値はコントロールパネルの利用量が正です",
    "きょうの月齢はおよそ17.4で",
    "日経平均株価は64362円で前日より2495円高いです",
    "1ドル157円40銭です",
    "横浜の日の出は4時50分、日の入りは18時47分です",
]


def measure(text: str):
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        out = f.name
    trace = out + ".trace"
    try:
        subprocess.run([app.OPENJTALK_BIN, "-x", app.OPENJTALK_DIC,
                        "-m", app.OPENJTALK_VOICE, "-ow", out, "-ot", trace],
                       input=text.encode("utf-8"), check=True,
                       stdout=subprocess.DEVNULL, timeout=30)
        raw = open(trace, encoding="utf-8", errors="replace").read()
        pace = app._trace_pace(trace)
    finally:
        for p in (out, trace):
            try:
                os.unlink(p)
            except OSError:
                pass
    body = raw.split("[Output label]")[1].split("[Global parameter]")[0]
    mora, aps = 0, []
    for line in body.strip().splitlines():
        m = re.match(r"^\d+ \d+ \S+?\-([^\+]+)\+", line)
        if not m or m.group(1) in ("sil", "pau"):
            continue
        if m.group(1) in app._VOWELS:
            mora += 1
        f = re.search(r"/F:(\d+)_", line)
        if f:
            aps.append(int(f.group(1)))
    return len(text), mora, (max(aps) if aps else 0), pace


print("%-34s %4s %5s %6s %8s" % ("text", "字", "モーラ", "最大句", "秒/モーラ"))
for t in CASES:
    n, mora, ap, pace = measure(t)
    mark = "NG" if (pace or 0) > 0.16 else "ok"
    print("%s %-32s %4d %5d %6d %8.3f" % (mark, t[:32], n, mora, ap, pace or 0))
