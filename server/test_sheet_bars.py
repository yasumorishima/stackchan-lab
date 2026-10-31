"""譜面からの歌詞の配りを、手元の形だけで確かめる（回線も譜面ファイルも要らない）。

①休符なしで 2 語が続くフレーズでも、語の切れ目を小節の頭に揃える
  （宮﨑の 0〜3 小節と同じ長さの並び。高さは使わないのでダミー）
②1 音に 2 モーラ詰めないと揃わない時は従来の配りのまま
③歌詞の注意書き（※…）は歌わない

  ./.venv/bin/python test_sheet_bars.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cheer_song
import sheet_song

ok = fail = 0


def check(name, got, want):
    global ok, fail
    good = got == want
    ok += good
    fail += not good
    print("%s %s: %r" % ("OK" if good else "NG", name, got))


def sheet_of(lens, start=0):
    notes, t = [], start
    for n in lens:
        if n is None:          # 休み 2 マス
            t += 2
            continue
        notes.append({"t16": t, "len16": n, "semi": 0})
        t += n
    return {"sec_per_bar": 1.27, "notes": notes}


def sung(sheet, lines, name="試験"):
    notes, _tempo, _nm, _call = sheet_song.build(sheet, lines, cheer_song.moras,
                                                 name)
    return [n[2] for n in notes if n[0] is not None]


# ① 宮﨑と同じ長さの並び: 0〜1 小節 = 7+4 音、2〜3 小節 = 7+3 音＋休み
# （後半の 4〜5 小節も同じ形。フレーズが 1 つだと全体一括の控えに回るので 2 つ置く）
lens = [2, 4, 2, 2, 2, 2, 2, 4, 4, 4, 4,
        2, 4, 2, 2, 2, 2, 2, 2, 4, 2, 6, None,
        2, 4, 2, 2, 2, 2, 2, 4, 4, 4, 4, None]
got = sung(sheet_of(lens), ["さあふりぬけみやざき きはくあふれるパワーで",
                            "ゆめえがけあざやかに"])
check("1 語目の伸ばしは 1 小節目の最後", got[10], "い")
check("2 語目は 2 小節目の頭から", got[11], "き")
check("2 語目の最後", got[21], "え")
check("後半も同じ形で伸ばす", got[-1], "い")

# ② 詰めないと揃わない時は、小節に揃えず従来どおり（全部で 1:1 になる）
lens2 = [2, 2, 2, 2, 2, 2, 2, 2, 4, 4, 4, 4]
got2 = sung(sheet_of(lens2), ["あいうえおかきくけ こさし"])
check("詰めが要る時は従来の配り", got2,
      ["あ", "い", "う", "え", "お", "か", "き", "く", "け", "こ", "さ", "し"])

# ③ 注意書きの行は歌わない
check("注意書きを落とす",
      sheet_song.split_call(["※代打での打席では使用しません", "いざすすめ",
                             "かっとばせー！○○！"]),
      (["いざすすめ"], "かっとばせー！○○！"))

# ④ 「！」で終わる最後の行はコール（神里「イーヤーサーサー ！かみざと！」）
check("！で終わる行はコール",
      sheet_song.split_call(["オーーーしまんちゅのほし", "イーヤーサーサー ！かみざと！"]),
      (["オーーーしまんちゅのほし"], "イーヤーサーサー ！かみざと！"))
check("筒香の掛け声も同じ形で落ちる（表と二重に落とさない）",
      sheet_song.drop_chant(sheet_song.split_call(
          ["さあ うてつつごう", "かっとばせ ホームラン", "ゴー！ゴー！つつごう！"])[0],
          "筒香嘉智_試験"),
      ["さあ うてつつごう", "かっとばせ ホームラン"])

# ⑤ 長い音はフレーズの終わり。次の語を長い音から始めない（捕手のテーマの形:
#    弱起 2＋1 小節目 5 音＋2 小節目頭の 12 マス、続く 9 音に 9 モーラ）
lens3 = [2, 2, 4, 4, 2, 4, 2, 12, 2, 2, 4, 4, 2, 2, 2, 2, 11, None,
         2, 2, 4, 4, 4, 4, 12]
sh3 = sheet_of(lens3, start=12)
got3 = sung(sh3, ["いざすすめ そのてでかじをとれ", "あいうえおかき"])
check("長い音は前の語の伸ばし", got3[7], "え")
check("次の語は長い音の後ろから", got3[8], "そ")
check("次の語の最後が次の長い音", got3[16], "れ")

# ⑥ 休符が 1 つも無い曲でも行を小節に揃える（梶原の形: 2 小節で 1 行）
lens4 = [4, 4, 4, 2, 2, 8, 4, 2, 2, 4, 4, 4, 2, 2, 8, 4, 2, 2]
got4 = sung(sheet_of(lens4), ["あいうえお かき", "さしすせそ たち"])
check("休符なしでも 2 行目は 2 小節目の頭から", got4[9], "さ")

print("%d/%d" % (ok, ok + fail))
sys.exit(1 if fail else 0)
