"""モーラ見積り _mora_est が実際の合成と何割ずれるかを測る。"""
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app                                        # noqa: E402

CASES = [
    "きょうは晴れです",
    "きょうの横浜は晴れで気温は33度です",
    "きょうの横浜は晴れで最高気温は33度です",
    "きょうの横浜は晴れで最高気温は33度で湿度は81%です",
    "残りはおよそ2996回です",
    "無料枠3000回の残りはおよそ2996回です",
    "今月このサーバーから使ったLLMリクエストは4回です",
    "日経平均株価は64362円で前日より2495円高いです",
    "台風13号ドルフィンは南鳥島近海にあって非常に強い勢力です",
    "ビットコインはおよそ993万円で前日から3%安いです",
    "きょうの月齢はおよそ17.4で満月をすぎて欠けはじめた月です",
    "横浜の日の出は4時50分で日の入りは18時47分です",
    "1ドル157円40銭です",
    "サーバーの外で使った分は数えられません",
    "熊本県天草芦北地方でマグニチュード2.3の地震がありました",
]


def actual(text):
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        out = f.name
    trace = out + ".trace"
    try:
        subprocess.run([app.OPENJTALK_BIN, "-x", app.OPENJTALK_DIC,
                        "-m", app.OPENJTALK_VOICE, "-ow", out, "-ot", trace],
                       input=text.encode("utf-8"), check=True,
                       stdout=subprocess.DEVNULL, timeout=30)
        raw = open(trace, encoding="utf-8", errors="replace").read()
    finally:
        for q in (out, trace):
            try:
                os.unlink(q)
            except OSError:
                pass
    body = raw.split("[Output label]")[1].split("[Global parameter]")[0]
    return sum(1 for line in body.strip().splitlines()
               for m in [re.match(r"^\d+ \d+ \S+?\-([^\+]+)\+", line)]
               if m and m.group(1) in app._VOWELS)


worst = 0.0
print("%5s %5s %6s  %s" % ("見積", "実際", "比", "text"))
for t in CASES:
    est, act = app._mora_est(t), actual(t)
    ratio = est / act if act else 0
    worst = max(worst, abs(ratio - 1))
    print("%5d %5d %6.2f  %s" % (est, act, ratio, t))
print("最大ずれ %.0f%%" % (worst * 100))
