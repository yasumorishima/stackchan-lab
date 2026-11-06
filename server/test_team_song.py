"""チーム曲の呼び方と、譜面に歌詞を乗せる所を確かめる（回線も本物の譜面も要らない）。

①「点が入った時の歌」「チャンステーマ1」「負けてる時の歌」から曲が決まる
②選手の応援歌の頼み方や、歌を頼んでいない文ではチーム曲を選ばない
③前奏（from_bar より前）は歌わず、「オオオオー」「かっとばせー」も落とさずに歌う
④返事の言い方（チーム曲は「◯◯の応援歌」と言わない）

  ./.venv/bin/python test_team_song.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cheer_song
import server_tools
import team_song

ok = fail = 0


def check(name, got, want):
    global ok, fail
    good = got == want
    ok += good
    fail += not good
    print("%s %s: %r" % ("OK" if good else "NG", name, got))


# ① 呼び方
for text, want in [("点が入った時の歌を歌って", "熱き星たちよ"),
                   ("得点の歌歌って", "熱き星たちよ"),
                   ("熱き星たちよ歌って", "熱き星たちよ"),
                   ("試合開始の歌", "勝利の輝き"),
                   ("負けてる時の歌歌って", "ライジングテーマ"),
                   ("ライジングテーマ流して", "ライジングテーマ"),
                   ("チャンステーマ1歌って", "チャンステーマ1"),
                   ("チャンステーマ 2 歌って", "チャンステーマ2"),
                   ("チャンステーマ歌って", team_song.DEFAULT_CHANCE),
                   ("横浜市歌を歌って", "横浜市歌"),
                   ("横浜ベイスターズ歌って", "熱き星たちよ")]:
    check(text, team_song.request(text), want)

# ② 選ばない
for text in ["牧の応援歌歌って", "宮﨑の歌", "得点圏打率は？", "今日は負けてるね",
             "度会の応援歌を歌って", "横浜ベイスターズの牧の応援歌歌って"]:
    check(text, team_song.request(text), None)

# ③ 譜面に乗せる（前奏 1 小節＋歌 2 小節の作り物）
with tempfile.TemporaryDirectory() as d:
    notes = [{"t16": 0, "len16": 16, "semi": 0}]                 # 前奏
    t = 16
    for _ in range(4):                                          # おおおおー
        notes.append({"t16": t, "len16": 4, "semi": 2})
        t += 4
    for ln in (4, 4, 4, 4):                                     # かっとばせ
        notes.append({"t16": t, "len16": ln, "semi": 4})
        t += ln
    with open(os.path.join(d, "試験曲.json"), "w", encoding="utf-8") as f:
        json.dump({"sec_per_bar": 1.28, "notes": notes}, f)
    with open(os.path.join(d, "team_songs.json"), "w", encoding="utf-8") as f:
        json.dump({"_note": "x", "熱き星たちよ": {
            "sheet": "試験曲", "from_bar": 1,
            "lines": ["おおおおー", "かっとばせ"]}}, f, ensure_ascii=False)
    check("歌える曲", team_song.titles(d), ["熱き星たちよ"])
    out, tempo = team_song.build("熱き星たちよ", d, cheer_song.moras)
    check("前奏を歌わない", out[0][1], 4)
    got = "".join(n[2] for n in out if n[0])
    check("おおおおー を落とさない", got.startswith("おおおおー"), True)
    check("かっとばせ を落とさない", got.endswith("とばせ"), True)
    check("テンポ", round(tempo), round(240 / 1.28))
    try:
        team_song.build("ライジングテーマ", d, cheer_song.moras)
        check("無い曲は LookupError", False, True)
    except LookupError:
        check("無い曲は LookupError", True, True)

# ⑤ 伸ばし方（音源から起こすと全部同じ長さになる。ー の音を長く配り直す）
src = [["C4", 30, "め"], ["D4", 30, "ざ"], [None, 10, ""], ["E4", 30, "せ"],
       ["F4", 30, "ほ"]]
got = cheer_song.restyle(src, ["めざせ", "ほ"], {"めざせ": "めざーせ"})
check("ー を長く", [(n[2], n[1]) for n in got],
      [("め", 25), ("ざー", 50), ("せ", 25), ("ほ", 30)])

# ⑥ 譜面に 1 音ずつ歌詞（同じ高さの「ー」は前に足す・高さが変わる「ー」は残す）
sh = {"sec_per_bar": 1.28, "notes": [
    {"t16": 2, "len16": 2, "semi": 7}, {"t16": 4, "len16": 8, "semi": 7},
    {"t16": 12, "len16": 2, "semi": 5}, {"t16": 16, "len16": 4, "semi": 4}]}
got, _t = cheer_song.per_note(sh, "ワ ー / せ ー")
check("1 音ずつ", [(n[2], n[1]) for n in got],
      [("ワ", 10), ("せ", 2), ("", 2), ("ー", 4)])

# ④ 返事の言い方
check("チーム曲", server_tools.song_label("熱き星たちよ"), "熱き星たちよ")
check("選手", server_tools.song_label("牧秀悟"), "牧秀悟の応援歌")

print("\n%d ok / %d ng" % (ok, fail))
sys.exit(1 if fail else 0)
