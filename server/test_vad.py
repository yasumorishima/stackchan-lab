"""VAD（vad_step）の単体試験。通信も本体も要らない。

2026-08-01 の OOM（無音だけだとどのカウンタも進まず、mode:auto の
受信バッファが際限なく育って RAM 7GB 超で kill ×2）の再発防止が主眼。

  ./.venv/bin/python test_vad.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app

MS = 60.0          # 実機のフレーム長
QUIET = 100.0      # 閾値未満の環境音
LOUD = 1000.0      # 実機の声の実測帯（rms 900〜1160）


def fresh():
    return {"speech_ms": 0.0, "silence_ms": 0.0, "buffered_ms": 0.0,
            "max_rms": 0.0}


def run(state, frames):
    """(rms, フレーム数) の列を流し、True が出たフレーム番号 or None を返す。"""
    n = 0
    for rms, count in frames:
        for _ in range(count):
            n += 1
            if app.vad_step(state, MS, rms):
                return n
    return None


def main():
    ok = True

    def check(memo, got, want):
        nonlocal ok
        good = got == want
        if not good:
            ok = False
        print("%-4s %-44s -> %s (期待 %s)" % ("OK" if good else "NG",
                                              memo, got, want))

    # 1. 無音だけでも上限で必ず切れる（OOM 再発防止の本丸）
    st = fresh()
    n = run(st, [(QUIET, 100000)])
    limit = int(app.VAD_MAX_MS / MS)
    check("純無音は VAD_MAX で切れる", n, limit)
    # 切れた時点で声は無い＝呼び出し側は捨てる判断ができる
    check("その時 speech_ms は 0", st["speech_ms"], 0.0)

    # 2. 普通の発話: 声のあと無音が続いたら切れる
    st = fresh()
    speech_frames = int(500 / MS) + 1                   # min 300ms を超える声
    silence_frames = int(app.VAD_SILENCE_MS / MS) + 1
    n = run(st, [(LOUD, speech_frames), (QUIET, 200)])
    check("発話→無音で切れる", n is not None, True)
    check("切れた時 speech_ms は min 以上",
          st["speech_ms"] >= app.VAD_MIN_SPEECH_MS, True)

    # 3. 上限の直前に話し始めても、発話は切られず続きを録れる
    st = fresh()
    almost = limit - 2
    n = run(st, [(QUIET, almost), (LOUD, 50)])
    # 無音掃き出し（speech_ms==0 の枝）では切れず、発話が min を超えて
    # 無音が来るまで続く
    check("上限際の発話開始は掃き出さない", n is None, True)
    check("発話としてカウントが進む", st["speech_ms"] >= app.VAD_MIN_SPEECH_MS,
          True)
    n = run(st, [(QUIET, 200)])
    check("その発話も無音で普通に切れる", n is not None, True)

    # 4. 長話は speech+silence の上限で打ち切る（従来挙動の維持）
    st = fresh()
    n = run(st, [(LOUD, 100000)])
    check("長話も VAD_MAX で打ち切る", n, limit)

    # 5. 短い物音だけ（min 未満）は発話成立せず、上限まで録って掃き出しへ
    st = fresh()
    n = run(st, [(QUIET, 20), (LOUD, 2), (QUIET, 100000)])
    check("物音だけでも必ずどこかで切れる", n is not None, True)
    check("その時 speech_ms は min 未満",
          st["speech_ms"] < app.VAD_MIN_SPEECH_MS, True)

    # 敷居を部屋の静けさに合わせる（user 実機 2026-08-08「なかなか反応しない」）。
    # 実測: 捨てた 3,290 件の中央 rms は 296 で、固定 500 のすぐ下に声が埋もれる
    st = fresh()
    run(st, [(60.0, 50)])                     # 静かな部屋を聞かせる
    check("静かな部屋では敷居が下がる", app.vad_threshold(st) <= 250.0, True)
    got = run(st, [(400.0, 10), (60.0, 20)])  # 小さい声（旧 500 では無視された）
    check("静かな部屋なら小さい声も発話になる", got is not None, True)
    check("その時 speech_ms が立つ", st["speech_ms"] >= app.VAD_MIN_SPEECH_MS, True)

    st = fresh()
    run(st, [(400.0, 200)])                   # うるさい部屋（テレビ等）
    check("うるさい部屋では敷居が上がる", app.vad_threshold(st) >= 1000.0, True)
    st2 = fresh()
    st2["floor"] = 400.0
    run(st2, [(900.0, 10), (400.0, 20)])
    check("うるさい部屋の中くらいの音は発話にしない",
          st2["speech_ms"] < app.VAD_MIN_SPEECH_MS, True)

    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
