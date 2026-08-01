"""Open JTalk が実際にどう読むかを音素列で見る（耳で聞かずに確かめる）。"""
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app                                        # noqa: E402

CASES = [
    "降水確率76%です",
    "風は0.5m/sです",
    "8月1日(土)はくもりです",
    "今月のLLMリクエストは4回です",
    "熊本で震度５弱の地震がありました",
    "熊本で震度5弱の地震がありました",
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
    seq, sec = [], 0.0
    for line in body.strip().splitlines():
        m = re.match(r"^(\d+) (\d+) \S+?\-([^\+]+)\+", line)
        if not m:
            continue
        sec += (int(m.group(2)) - int(m.group(1))) / 1e7
        seq.append(m.group(3))
    return seq, sec


for t in CASES:
    seq, sec = phones(t)
    body = [p for p in seq if p not in ("sil", "pau")]
    mora = sum(1 for p in body if p in app._VOWELS)
    print("%s  ->  %.2f秒 / %dモーラ / %.3f秒毎" %
          (t, sec, mora, (sec / mora if mora else 0)))
    print("    " + " ".join(seq))
