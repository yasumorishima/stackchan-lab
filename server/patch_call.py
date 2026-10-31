# -*- coding: utf-8 -*-
"""音源から歌う経路でも「かっとばせー！○○！」を歌わない。

コールは歌ではないので音として付けない（user 指示）。ところがこの決まりを
守っていたのは**譜面から歌う経路だけ**で、音源から歌う 12 曲は歌詞を全部
つないでいたためコールまで歌っていた（実機 2026-08-08「かっとばせーも
入ってるし」。実測 宮﨑は 44 モーラ＝コール 9 モーラ込み）。

判定は `sheet_song.split_call` を共用する（同じ決まりを 2 か所に書かない）。

**ついでに直さない**: 「パワー」が「ぱわ」で渡るのは設計どおり（`moras` が
「ワー」を 1 モーラにまとめ、伸ばす分は音符の長さで表す）。
"""
import io
import sys

P = "/home/yasu/stackchan-server/cheer_song.py"
src = io.open(P, encoding="utf-8").read()

OLD = '''    text = "".join(songs.get(key, [])) if key else ""
    if not text:
        raise LookupError("歌詞が無い")
'''
NEW = '''    text = "".join(songs.get(key, [])) if key else ""
    if not text:
        raise LookupError("歌詞が無い")
    # 最後の「かっとばせー！○○！」はコール＝歌ではないので歌わない。
    # 譜面から歌う経路（sheet_song.build）と同じ判定を使う
    sung_lines, _call = sheet_song.split_call(list(songs.get(key, [])))
    text = "".join(sung_lines) or text
'''

if "sung_lines, _call = sheet_song.split_call" in src:
    sys.exit("すでに適用済み")
if src.count(OLD) != 1:
    sys.exit("目印が 1 つでない (%d)" % src.count(OLD))
src = src.replace(OLD, NEW)

if "\nimport sheet_song" not in src:
    anchor = "\nimport server_tools"
    if src.count(anchor) != 1:
        sys.exit("import の目印が 1 つでない")
    src = src.replace(anchor, "\nimport server_tools\nimport sheet_song", 1)

io.open(P, "w", encoding="utf-8").write(src)
print("patched")
