# -*- coding: utf-8 -*-
"""同じ高さの音が 1 マスの休みだけ挟んで並んでいたら 1 つにまとめる。

user 実機指摘（2026-08-08）「ミートとお」。実測すると旋律側が細切れだった:

     8  A#4 len=2  'ト'
     9  休み len=3
    10  G#4 len=1  'と'   ← 1 マスだけの音
    11  休み len=1        ← 1 マスだけの休み
    12  G#4 len=1  'お'   ← 同じ高さの 1 マス（余りを埋めた母音）

本来 `G#4 len=3` の 1 音。読み取りで割れたところへ余りの母音を配ったので
「とお」と聞こえた。**歌詞側ではなく譜面の読みの問題**。

同じ高さ・間の休みが 1 マス以下なら繋ぐ（`GLUE_GAP`）。譜面の音の長さの合計は
変えない（休みぶんも音に含める＝もともと 1 音だったものを戻すだけ）。
"""
import io
import sys

P = "/home/yasu/stackchan-server/sheet_song.py"
s = io.open(P, encoding="utf-8").read()
if "def glue_split" in s:
    sys.exit("すでに適用済み")

old = "def _phrases(notes):"
new = '''GLUE_GAP = 1              # この長さ以下の休みなら、同じ高さの音を繋ぐ


def glue_split(notes):
    """読み取りで割れた同じ高さの音を 1 つに戻す。

    1 マスの休みを挟んで同じ高さが並ぶのは、譜面の帯を横切る線で切れただけ
    のことが多い（実測 柴田: `G#4 len=1` 休み1 `G#4 len=1` ＝本来 `len=3`）。
    そこへ歌詞の余りが配られて「とお」のような音が生まれていた。
    """
    out = []
    for n in notes:
        if out:
            prev = out[-1]
            gap = n["t16"] - (prev["t16"] + prev["len16"])
            if prev["semi"] == n["semi"] and 0 <= gap <= GLUE_GAP:
                prev["len16"] = n["t16"] + n["len16"] - prev["t16"]
                continue
        out.append(dict(n))
    return out


def _phrases(notes):'''
if s.count(old) != 1:
    sys.exit("_phrases の目印が 1 つでない (%d)" % s.count(old))
s = s.replace(old, new, 1)

old2 = '    notes = sorted(sheet["notes"], key=lambda n: n["t16"])'
new2 = ('    notes = glue_split(sorted(sheet["notes"], key=lambda n: n["t16"]))')
if s.count(old2) != 1:
    sys.exit("build の目印が 1 つでない (%d)" % s.count(old2))
s = s.replace(old2, new2)
io.open(P, "w", encoding="utf-8").write(s)
print("patched sheet_song.py")
