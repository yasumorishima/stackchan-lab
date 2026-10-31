# -*- coding: utf-8 -*-
"""整形前後で Open JTalk が実際にどう読むかを形態素解析の結果で確かめる。"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app  # noqa: E402

CASES = ["3 000 回", "1 か月", "AI Engine", "ナスダック（NASDAQ）", "28℃"]

DIC = os.environ.get("OJT_DIC", "/var/lib/mecab/dic/open-jtalk/naist-jdic")
VOICE = os.environ.get("OJT_VOICE",
                       "/usr/share/hts-voice/nitech-jp-atr503-m001/nitech_jp_atr503_m001.htsvoice")


def yomi(text):
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "in.txt")
        trace = os.path.join(d, "trace.txt")
        with open(src, "w", encoding="utf-8") as f:
            f.write(text)
        subprocess.run(["open_jtalk", "-x", DIC, "-m", VOICE,
                        "-ot", trace, "-ow", os.path.join(d, "out.wav"), src],
                       check=True, capture_output=True)
        with open(trace, encoding="utf-8", errors="replace") as f:
            body = f.read()
    out = []
    for line in body.splitlines():
        if "\t" not in line:
            continue
        surface, feat = line.split("\t", 1)
        cols = feat.split(",")
        if len(cols) > 9 and cols[9] not in ("*", ""):
            out.append(cols[9])
        elif surface.strip():
            out.append("<%s>" % surface)
    return " ".join(out)


for c in CASES:
    print("原文   %-18s -> %s" % (c, yomi(c)))
    fixed = app.fix_reading(c)
    print("整形後 %-18s -> %s" % (fixed, yomi(fixed)))
    print("")
