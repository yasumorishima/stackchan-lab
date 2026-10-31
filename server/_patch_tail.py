# -*- coding: utf-8 -*-
"""末尾の掛け声も曲ごとに登録できるようにする。

user 指摘（2026-08-08）: 筒香の歌は「さあ打て」から「かっとばせ ホームラン」
まで。末尾の「ゴー！ゴー！つつごう！」は掛け声。

これまで末尾で外していたのは「かっとばせー！○○！」の形だけ（split_call）。
形が違う掛け声は残っていた。**歌が終わる行も曲ごとの表に持つ**。
"""
import io
import sys

P = "/home/yasu/stackchan-server/sheet_song.py"
s = io.open(P, encoding="utf-8").read()
if "TAIL_CHANT" in s:
    sys.exit("すでに適用済み")

old = 'LEAD_CHANT = {"牧秀悟": 4, "筒香嘉智": 3}'
new = '''LEAD_CHANT = {"牧秀悟": 4, "筒香嘉智": 3}
# 末尾の掛け声を落とす行数（「かっとばせー！○○！」以外の形。split_call が
# 落としたあとの行数で数える）。筒香: 「ゴー！ゴー！つつごう！」
TAIL_CHANT = {"筒香嘉智": 1}'''
if s.count(old) != 1:
    sys.exit("LEAD_CHANT の目印が 1 つでない (%d)" % s.count(old))
s = s.replace(old, new, 1)

old2 = '''    lead = LEAD_CHANT.get(name)
    if lead:
        return list(lines[lead:]) or list(lines)'''
new2 = '''    lead = LEAD_CHANT.get(name)
    tail = TAIL_CHANT.get(name, 0)
    if lead or tail:
        got = list(lines[lead or 0:len(lines) - tail if tail else None])
        return got or list(lines)'''
if s.count(old2) != 1:
    sys.exit("drop_chant の目印が 1 つでない (%d)" % s.count(old2))
s = s.replace(old2, new2)
io.open(P, "w", encoding="utf-8").write(s)
print("patched sheet_song.py")
