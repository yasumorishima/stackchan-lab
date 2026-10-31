# -*- coding: utf-8 -*-
"""譜面があるならそれで歌う（音源からの採譜より先に見る）。

これまでは「音源が無い選手の受け皿」だったが、音源からの採譜には自前の音高
追跡が入る（実測: 宮﨑の楽譜にオクターブの飛び値が残る／楽譜 43 音に対して
鳴った音 62）。譜面はゲームの応援歌エディタに書かれた高さと長さそのもので、
読み取りは音名ラベルの数と 2 周ぶんの一致で検証してある。

**置いてあること自体が「読めた」の印**にする＝繰り返しの見立てが怪しい曲
（柴田 0.421 / 関根 0.420 / 牧 0.327 / 筒香 0.185・神里は校正が落ちる）は
`cache/sheets/` に置かない。判定を条件式でなく置き場で表す。
"""
import io
import sys

P = "/home/yasu/stackchan-server/cheer_song.py"
src = io.open(P, encoding="utf-8").read()

OLD = """    # 置いてもらった音源が最優先（配布ページに無い選手はこれしか手が無い）
    path = local_audio(key)
"""
NEW = """    # 譜面（ゲームの応援歌エディタ）から起こした旋律があるならそれを使う。
    # 音源からの採譜は自前の音高追跡が入るぶん誤差が残る（実測 宮﨑: 楽譜に
    # オクターブの飛び値／楽譜 43 音に対し鳴った音 62）のに対し、譜面は高さと
    # 長さが離散値で書いてある。**ここに置いてあること自体が「読めた」の印**で、
    # 繰り返しの見立てが怪しい曲は置かない（判定を置き場で表す）
    if sheet_source(key):
        return await _prepare_sheet(key, songs)

    # 置いてもらった音源（配布ページに無い選手はこれしか手が無い）
    path = local_audio(key)
"""

if "if sheet_source(key):\n        return await _prepare_sheet" in src:
    sys.exit("すでに適用済み")
if src.count(OLD) != 1:
    sys.exit("目印が 1 つでない (%d)" % src.count(OLD))
io.open(P, "w", encoding="utf-8").write(src.replace(OLD, NEW))
print("patched")
