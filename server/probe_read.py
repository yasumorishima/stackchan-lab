"""Open JTalk が実際にどう読むかを音素列で見る（耳で聞かずに確かめる）。

引数を渡せばその文を、無ければ既定の確認用の文を測る。
"""
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app                                        # noqa: E402

CASES = sys.argv[1:] or [
    "降水確率76%です",
    "風は0.5m/sです",
    "8月1日(土)はくもりです",
    "今月のLLMリクエストは4回です",
    "熊本で震度５弱の地震がありました",
    "エスアンドピー500は7490ポイントです",
]


def phones(text: str):
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
        for p in (out, trace):
            try:
                os.unlink(p)
            except OSError:
                pass
    body = raw.split("[Output label]")[1].split("[Global parameter]")[0]
    seq = []
    for line in body.strip().splitlines():
        m = re.match(r"^\d+ \d+ \S+?\-([^\+]+)\+", line)
        if m:
            seq.append(m.group(1))
    return seq


for t in CASES:
    seq = phones(t)
    mora = sum(1 for p in seq if p in app._VOWELS)
    print("%s  ->  実際 %d モーラ / 見積り %d" % (t, mora, app._mora_est(t)))
    print("    " + " ".join(seq))
