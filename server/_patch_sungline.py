# -*- coding: utf-8 -*-
"""実際の歌い方を曲ごとの表に持つ（公式歌詞の表記と歌い方は違う）。

user 指摘（2026-08-08）「ねらーいすましたミートと」。公式歌詞は
`ねらいすました ミートと` で長音記号が無く、こちらは「ねらい」を 3 モーラで
配っていた。実際は「ねらーい」と伸ばして歌う。**文字からは導けない**ので、
掛け声の表（LEAD_CHANT）と同じく、教えていただいた歌い方をそのまま持つ。

SUNG_LINES[選手名] = [歌う行, ...]。指定があればその行で置き換える
（行数は公式歌詞と同じでなくてよい。歌う中身がこれで確定する）。
"""
import io
import sys

P = "/home/yasu/stackchan-server/sheet_song.py"
s = io.open(P, encoding="utf-8").read()
if "SUNG_LINES" in s:
    sys.exit("すでに適用済み")

old = 'LEAD_CHANT = {"牧秀悟": 4}'
new = '''LEAD_CHANT = {"牧秀悟": 4}
# 🔴 実際の歌い方は公式歌詞の表記と違う（長音の位置など）。文字からは導けない
# ので、教えていただいた歌い方をそのまま持つ。指定があれば歌詞を置き換える。
# 柴田: 公式は「ねらいすました ミートと」だが、歌は「ねらーいすましたミートと」
SUNG_LINES = {
    "柴田竜拓": ["ねらーいすましたミートと", "たくみな グラブさばきは",
                 "どりょくという かけらの", "けっしょうの あかしだ"],
}'''
if s.count(old) != 1:
    sys.exit("LEAD_CHANT の目印が 1 つでない (%d)" % s.count(old))
s = s.replace(old, new, 1)

old2 = "    sung_lines = drop_chant(sung_lines, name)"
new2 = ('    sung_lines = SUNG_LINES.get(name) or drop_chant(sung_lines, name)')
if s.count(old2) != 1:
    sys.exit("build の目印が 1 つでない (%d)" % s.count(old2))
s = s.replace(old2, new2)
io.open(P, "w", encoding="utf-8").write(s)
print("patched sheet_song.py")

Q = "/home/yasu/stackchan-server/cheer_song.py"
t = io.open(Q, encoding="utf-8").read()
o = "    sung_lines = sheet_song.drop_chant(sung_lines, key)"
n = "    sung_lines = (sheet_song.SUNG_LINES.get(key)\n                  or sheet_song.drop_chant(sung_lines, key))"
if t.count(o) != 1:
    sys.exit("cheer_song の目印が 1 つでない (%d)" % t.count(o))
io.open(Q, "w", encoding="utf-8").write(t.replace(o, n))
print("patched cheer_song.py")
