"""相槌ゲート（worth_answering）の単体試験。通信も本体も要らない。

話しかけていないのに勝手に喋る問題（2026-08-01 user 指摘）の再発防止。
環境音が「うん」「あっ」と書き起こされても、こちらが話した直後でなければ
返事をしない。

  ./.venv/bin/python test_filler.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app

OLD = 999.0    # 前回の発話からずっと経っている
JUST = 5.0     # こちらが話した直後（FILLER_FOLLOWUP_SEC 以内）

CASES = [
    # (認識結果, 前回発話からの秒数, 返事をすべきか, メモ)
    ("うん", OLD, False, "唐突な相槌は無視"),
    ("うん", JUST, True, "こちらの発話直後の相槌は返事"),
    ("はあ", OLD, False, "ため息も無視"),
    ("あっ。", OLD, False, "句読点付きの相槌も無視"),
    ("はい！", OLD, False, "記号付きの相槌も無視"),
    ("うーん", OLD, False, "長音の相槌も無視"),
    ("ん", OLD, False, "一文字は無視"),
    ("", OLD, False, "空認識は無視"),
    ("　。！", OLD, False, "記号だけも無視"),
    ("ドル円いくら", OLD, True, "普通の質問は返事"),
    ("寒い", OLD, True, "二文字でも相槌でなければ返事"),
    ("話しかけてないのに", OLD, True, "文になっていれば返事"),
    ("こんにちは", OLD, True, "挨拶は返事"),
]


def main():
    ok = True
    for text, age, want, memo in CASES:
        got = app.worth_answering(text, age)
        mark = "OK" if got == want else "NG"
        if got != want:
            ok = False
        print("%-4s %-24s (%3.0fs) -> %-5s (期待 %s)  %s"
              % (mark, repr(text), age, got, want, memo))
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
