"""句読点なしのかたまりが何モーラから伸びるか（膝）を測る。"""
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app                                        # noqa: E402

CASES = [
    "きょうは晴れです",
    "きょうの横浜は晴れです",
    "きょうの横浜は晴れで気温は33度です",
    "きょうの横浜は晴れで最高気温は33度です",
    "きょうの横浜は晴れで最高気温は33度で湿度は81%です",
    "きょうの横浜は晴れで最高気温は33度で湿度は81%で風は弱いです",
    "きょうの横浜は晴れで最高気温は33度で湿度は81%で風は0.5メートルです",
    "残りはおよそ2996回です",
    "無料枠3000回の残りはおよそ2996回です",
    "今月このサーバーから使ったLLMリクエストは4回です",
    "日経平均株価は64362円で前日より2495円高いです",
    "台風13号ドルフィンは南鳥島近海にあって非常に強い勢力です",
    "台風13号ドルフィンは南鳥島近海にあって非常に強い勢力で西北西へ時速25キロで進んでいます",
]


def measure(text):
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
    mora = 0
    for line in body.strip().splitlines():
        m = re.match(r"^\d+ \d+ \S+?\-([^\+]+)\+", line)
        if m and m.group(1) in app._VOWELS:
            mora += 1
    return mora, pace


print("%5s %5s %8s  %s" % ("字", "モーラ", "秒/モーラ", "text"))
for t in CASES:
    mora, pace = measure(t)
    mark = "NG" if (pace or 0) > 0.16 else "ok"
    print("%s %3d %5d %8.3f  %s" % (mark, len(t), mora, pace or 0, t))
