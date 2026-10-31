# -*- coding: utf-8 -*-
"""音源から歌う曲は、全歌詞で対応付けてから歌う範囲だけ切り出す。

筒香を「さあ打て」〜「かっとばせ ホームラン」に絞ったら歌えなくなった
（16→15 曲）。音源は 32 秒ぶん（掛け声込みの全曲）あるのに歌詞だけ 39 モーラに
減らしたので、1 音 0.83 秒となり「歌詞と音源が対応していない」（0.8 秒）に
引っかかった。

**掛け声を歌詞から外すなら、音源側もその部分を使わない**のが正しい。全歌詞で
DP の対応を取ってから、歌う行に当たる音符だけを切り出す（旋律はそのまま）。
"""
import io
import sys

P = "/home/yasu/stackchan-server/cheer_song.py"
s = io.open(P, encoding="utf-8").read()
if "歌う範囲だけ切り出す" in s:
    sys.exit("すでに適用済み")

old = '''    morae = moras(text)
    if not morae:
        raise LookupError("歌詞が読めない")
    notes, tempo = await notes_by_morae(key, path, morae)'''
new = '''    # 🔴 音源は掛け声込みの全曲ぶんある。**全歌詞で対応付けてから歌う範囲だけ
    # 切り出す**（歌詞だけ減らすと 1 音が伸びて「対応していない」で弾かれる。
    # 実測 2026-08-08: 筒香が 39 モーラ / 32 秒 = 0.83 秒で歌えなくなった）
    all_lines = list(songs.get(key, []))
    all_morae = moras("".join(all_lines))
    morae = moras(text)
    if not morae:
        raise LookupError("歌詞が読めない")
    head = 0
    for ln in all_lines:
        if ln in sung_lines:
            break
        head += len(moras(ln))
    tail = 0
    for ln in reversed(all_lines):
        if ln in sung_lines:
            break
        tail += len(moras(ln))
    if head or tail:
        notes, tempo = await notes_by_morae(key, path, all_morae)
        end = len(notes) - tail if tail else len(notes)
        notes = notes[head:end]
        log.info("%s: 掛け声を除いて %d〜%d 音を歌う（全 %d モーラ）",
                 key, head, end, len(all_morae))
    else:
        notes, tempo = await notes_by_morae(key, path, morae)'''
if s.count(old) != 1:
    sys.exit("目印が 1 つでない (%d)" % s.count(old))
s = s.replace(old, new)
io.open(P, "w", encoding="utf-8").write(s)
print("patched cheer_song.py")
