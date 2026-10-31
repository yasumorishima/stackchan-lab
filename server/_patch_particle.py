# -*- coding: utf-8 -*-
"""助詞の「は」「へ」を、歌うときだけ「わ」「え」に読み替える。

user 実機指摘（2026-08-08）「グラブ捌きは、が わ でなくて は になってる」。
公式歌詞は仮名で書かれているので、助詞の「は」がそのまま「ha」と歌われる。

**語末・行末の「は」「へ」だけ**読み替える（語中の「は」は「はしる」「はやく」
のように普通の音なので触らない）。実測で該当は 4 曲:

    柴田   'たくみな グラブさばきは'
    関根   'はしりだす そのさきには'
    三森   'ほこらしきひとふりは'（音源が無く歌わない）
    蝦名   'うてとわのほしへ'（音源が無く歌わない）
"""
import io
import sys

P = "/home/yasu/stackchan-server/sheet_song.py"
s = io.open(P, encoding="utf-8").read()
if "read_particles" in s:
    sys.exit("すでに適用済み")

old = "def drop_chant(lines, name=\"\"):"
new = '''# 助詞の「は」「へ」は「わ」「え」と歌う。語中の「は」（はしる 等）は触らない
_PARTICLE_RE = re.compile(r"は(?=\s|$)")
_PARTICLE_HE_RE = re.compile(r"へ(?=\s|$)")


def read_particles(line):
    """歌うときの読みに直す。語末・行末の「は」→「わ」、「へ」→「え」。"""
    return _PARTICLE_HE_RE.sub("え", _PARTICLE_RE.sub("わ", line))


def drop_chant(lines, name=""):'''
if s.count(old) != 1:
    sys.exit("drop_chant の目印が 1 つでない (%d)" % s.count(old))
s = s.replace(old, new, 1)

old2 = "    sung_lines = drop_chant(sung_lines, name)"
new2 = ('    sung_lines = drop_chant(sung_lines, name)\n'
        '    # 助詞の「は」「へ」は「わ」「え」と歌う（user 指摘 2026-08-08）\n'
        '    sung_lines = [read_particles(x) for x in sung_lines]')
if s.count(old2) != 1:
    sys.exit("build の目印が 1 つでない (%d)" % s.count(old2))
s = s.replace(old2, new2)
io.open(P, "w", encoding="utf-8").write(s)
print("patched sheet_song.py")

# 音源経路にも通す
Q = "/home/yasu/stackchan-server/cheer_song.py"
t = io.open(Q, encoding="utf-8").read()
o = "    sung_lines = sheet_song.drop_chant(sung_lines, key)"
n = ('    sung_lines = sheet_song.drop_chant(sung_lines, key)\n'
     '    sung_lines = [sheet_song.read_particles(x) for x in sung_lines]')
if t.count(o) != 1:
    sys.exit("cheer_song の目印が 1 つでない (%d)" % t.count(o))
io.open(Q, "w", encoding="utf-8").write(t.replace(o, n))
print("patched cheer_song.py")
