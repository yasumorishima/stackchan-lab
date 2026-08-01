"""読み上げ前の後始末（clean_reply / shorten_reply）の単体試験。

  ./.venv/bin/python test_clean.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app

CLEAN = [
    ("</tool_call>", ""),
    ("こんにちは。", "こんにちは。"),
    ('はい<tool_call>{"name": "get_weather", "arguments": {}}', "はい"),
    ('あしたは晴れです。{"name": "x", "arguments": {"a": 1}} おわり',
     "あしたは晴れです。 おわり"),
    ('途中で切れた {"name": "get_weather"', "途中で切れた"),
    ("", ""),
    ("21時15分です。", "21時15分です。"),
    # 発話に添えた時刻をそのまま書き写してくる（qwen2.5:3b の実測）
    ("どういたしまして。（2026年7月31日(金) 7時00分)", "どういたしまして。"),
    ("（2026年7月31日(金) 7時00分）おはよう。", "おはよう。"),
    ("こんにちは。(7月31日)", "こんにちは。"),
    ("いま (7時00分) です。", "いま です。"),
    # 曜日を書き損じることもある（実測で "(now)" になった）
    ("横浜はくもりです。（2026年7月30日(now))", "横浜はくもりです。"),
    # gpt-oss-120b はコロン形式で書き写す（さくら実測: "(20:23) どうしましたか？"）
    ("(20:23) どうしましたか？", "どうしましたか？"),
    ("いま（20:23:45）です。", "いまです。"),
    # 日付形式でも書き写す（実機会話の実測。読み上げが数秒延びる実害あり）
    ("(2026-07-31 21:27) 了解です！", "了解です！"),
    ("(2026-07-31) こんにちは。", "こんにちは。"),
    ("（2026/07/31 21:27）どうぞ。", "どうぞ。"),
    # 比のような表現は消さない（時刻の括弧書きだけを狙う）
    ("スコアは(3対2)でした。", "スコアは(3対2)でした。"),
    # 時刻を聞かれた答えは残す（括弧で囲まれていない）
    ("いまは7時00分です。", "いまは7時00分です。"),
    # 日時でない丸括弧は消さない
    ("これは（大阪）の天気です。", "これは（大阪）の天気です。"),
    ("最高32.8度(晴れ)です。", "最高32.8度(晴れ)です。"),
    # 箇条書きと改行は声に出ない
    ("- 晴れです" + chr(10) + "- 32度です", "晴れです 32度です"),
    ("1. 晴れ" + chr(10) + "2. 32度", "晴れ 32度"),
    ("## 天気" + chr(10) + "晴れです。", "天気 晴れです。"),
    ("・晴れ", "晴れ"),
    # 行頭のマイナスは気温の符号かもしれない。空白が続く時だけ箇条書き
    ("-3度です。", "-3度です。"),
    ("-3度から-1度です。", "-3度から-1度です。"),
    ("- 3度です。", "3度です。"),
]

SHORTEN = [
    ("こんにちは。", "こんにちは。", "そのまま"),
    ("晴れです。32度です。降水確率は41%です。", "晴れです。32度です。", "3 文目を落とす"),
    ("晴れです。32度です。", "晴れです。32度です。", "2 文はそのまま"),
    ("はい。", "はい。", "1 文"),
    ("", "", "空"),
    # 1 文が長すぎる場合は読点で切る
    ("あ" * 100 + "、" + "い" * 100 + "。", "あ" * 100 + "、", "長い 1 文は読点で切る"),
    # 読点が早すぎる位置にしか無ければ字数で切る
    ("あ、" + "い" * 300, "あ、" + "い" * (app.MAX_REPLY_CHARS - 2) + "。",
     "読点が早すぎるなら字数で切る"),
]

ng = 0
print("== clean_reply ==")
for text, want in CLEAN:
    got = app.clean_reply(text)
    mark = "OK" if got == want else "NG"
    ng += got != want
    print("%s %-46r -> %r" % (mark, text[:46], got))
    if got != want:
        print("   期待: %r" % want)

print("== shorten_reply ==")
for text, want, memo in SHORTEN:
    got = app.shorten_reply(text)
    mark = "OK" if got == want else "NG"
    ng += got != want
    print("%s %-24s 入力%3d字 -> %3d字" % (mark, memo, len(text), len(got)))
    if got != want:
        print("   期待: %r" % want[:80])
        print("   実際: %r" % got[:80])

print("== split_long_runs ==")
SPLIT = [
    # 短文はそのまま 1 片
    ("今のドル円は1ドル＝157円40銭です。",
     ["今のドル円は1ドル＝157円40銭です。"], "短文は分けない"),
    # 実機で 23.6 秒スローモーションになった応答。「教えてもらえる」の語中の
    # 「も」では切らず、「必要か」の後（直後が漢字）で切る
    ("またはドルや他の通貨への換算が必要か教えてもらえるとお手伝いしやすいです",
     ["またはドルや他の通貨への換算が必要か", "教えてもらえるとお手伝いしやすいです"],
     "語境界で切る（語中の「も」は避ける）"),
    # 平仮名だけの長い連続は二文字助詞「ので」の後で切る
    ("きょうはとてもよいてんきなのでこうえんまでゆっくりさんぽをしてきました",
     ["きょうはとてもよいてんきなので", "こうえんまでゆっくりさんぽをしてきました"],
     "平仮名連続は「ので」の後"),
]
n_split = 0
for text, want, memo in SPLIT:
    n_split += 1
    got = app.split_long_runs(text)
    mark = "OK" if got == want else "NG"
    ng += got != want
    print("%s %s: %s" % (mark, memo, " | ".join(got)))
    if got != want:
        print("   期待: %s" % " | ".join(want))
# 不変条件: 連結すると元に戻る／どの片の呼気段落も上限以下
INVARIANT = [t for t, _w, _m in SPLIT] + [
    "970円のことですね！何に関する金額か、またはドルや他の通貨への換算が必要か教えてもらえるとお手伝いしやすいです♪",
    "",
    "こんにちは！",
]
import re as _re
for text in INVARIANT:
    n_split += 1
    segs = app.split_long_runs(text)
    joined = "".join(segs)
    runs_ok = all(len(r) <= app.OJT_MAX_RUN
                  for s in segs for r in _re.split("[" + app.OJT_PAUSES + "]", s))
    good = joined == text and runs_ok and all(segs)
    ng += not good
    print("%s 不変条件: %r" % ("OK" if good else "NG", text[:24]))

# 合成の前後に付く固定の無音を落とす処理（末尾の間延び対策）
import array as _arr
_rate = app.DOWN_RATE
_noise = [30, -30] * int(_rate * 0.2)          # パディングは真の無音でなく微小ノイズ
_TRIM = [
    (_arr.array("h", _noise + [4000, -4000] * int(_rate * 0.25)
                + [30, -30] * int(_rate * 0.3)).tobytes(),
     0.5 + app.OJT_KEEP_HEAD + app.OJT_KEEP_TAIL, "前後の無音を落とす"),
    (_arr.array("h", _noise + [80, -80] * int(_rate * 0.1)
                + [30, -30] * int(_rate * 0.3)).tobytes(),
     0.2 + app.OJT_KEEP_HEAD + app.OJT_KEEP_TAIL, "弱い音（振幅80）は削らない"),
]
for _pcm, _want, _memo in _TRIM:
    n_split += 1
    _got = len(app._trim_silence(_pcm)) / 2.0 / _rate
    _good = abs(_got - _want) < 0.02
    ng += not _good
    print("%s %s: %.3fs (期待 %.3fs)"
          % ("OK" if _good else "NG", _memo, _got, _want))

n_split += 1
_quiet = _arr.array("h", [5, -5] * int(_rate * 0.2)).tobytes()
_good = app._trim_silence(_quiet) == _quiet
ng += not _good
print("%s 全部無音なら触らない（0 バイトにしない）" % ("OK" if _good else "NG"))

n_split += 1
_odd = _arr.array("h", [0] * 10).tobytes() + b""
_good = app._trim_silence(_odd) == _odd
ng += not _good
print("%s 16bit として読めない列は触らない（例外にしない）" % ("OK" if _good else "NG"))

# 数字の途中で切らない（「29」を「2」「9」に割ると読みが壊れる）
_DIGIT = [
    ("は" + "0123456789" * 3 + "です", "先頭近くから始まる数字"),
    ("0123456789" * 4, "全部が数字（切り所が無い）"),
]
for _t, _memo in _DIGIT:
    n_split += 1
    _segs = app.split_long_runs(_t)
    _joined = "".join(_segs)
    _mid = any(_s[-1].isdigit() and _n[0].isdigit()
               for _s, _n in zip(_segs, _segs[1:]))
    _good = _joined == _t and not _mid   # どちらの場合も数字は割らない
    ng += not _good
    print("%s %s: %s" % ("OK" if _good else "NG", _memo,
                         " | ".join(s[:14] for s in _segs)))

total = len(CLEAN) + len(SHORTEN) + n_split
print("")
print("%d/%d 正解" % (total - ng, total))
sys.exit(1 if ng else 0)
