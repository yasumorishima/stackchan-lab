"""本番と同じ経路で歌わせて、**リズム**と**高さ**を別々に測る。

`cheer_song.prepare`（歌詞のモーラ数に合わせて DP で区切る）→ `sing_vv.sung`。

⚠️ 2026-08-06 まで、この試験は Sinsy 期の `sing.py` を呼んでいて**本番の経路を
測っていなかった**（しかも `sing` を import せずに呼ぶ行があり NameError で
落ちた）。さらに判定が「出来た音の高さの並び 対 元の音源の高さの並び」＝
**通しのずれ**だけで、内訳を見ていなかった。実測で分けると
**採譜 1.30 / 合成 0.58 / 通し 1.67 半音**（宮﨑）。通しの値が大きいのは、
比べる相手の「元の旋律」を**旧方式（高さの変わり目で畳む）**で作っていて、
音の数が食い違う（元 43 音 / 楽譜 31 音）ためで、歌が外れているのとは違う。

そこで判定は意味のある 2 つにする:
  ①リズム = 音符の始まりが音源の立ち上がりに乗っているか。
            **でたらめに置いた対照より小さいこと**（物差しは eval_onset）
  ②高さ   = 楽譜どおりの高さで歌えているか（合成のずれ）

  WHO=牧 ./.venv/bin/python test_transcribe.py
"""
import asyncio
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import aiohttp

import cheer_song
import eval_onset
import segment_dp
import sing_vv
import transcribe

WHO = os.environ.get("WHO", "宮﨑")
MAX_SEMI = float(os.environ.get("MAX_SEMI", "1.5"))
RHYTHM_MARGIN = float(os.environ.get("RHYTHM_MARGIN", "0.7"))
ok = fail = 0


def check(name, cond, got=""):
    global ok, fail
    if cond:
        ok += 1
        print("OK %s %s" % (name, got))
    else:
        fail += 1
        print("NG %s %s" % (name, got))


def contour(x, rate):
    """音の高さの並び（同じ高さが続く所を 1 音に畳む）。"""
    semi = transcribe.smooth(transcribe.fix_octaves(
        transcribe.f0_track(x, rate)))
    out = []
    for pitch, _a, _b in transcribe.merge_same(transcribe.segment(semi)):
        v = int(round(pitch))
        if not out or out[-1] != v:
            out.append(v)
    return out


def align_cost(a, b):
    inf = float("inf")
    d = [[inf] * (len(b) + 1) for _ in range(len(a) + 1)]
    d[0][0] = 0.0
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            d[i][j] = min(d[i - 1][j - 1], d[i - 1][j],
                          d[i][j - 1]) + abs(a[i - 1] - b[j - 1])
    return d[len(a)][len(b)] / max(len(a), len(b))


async def main():
    async with aiohttp.ClientSession() as s:
        print("本番と同じ経路で音符を用意する…")
        notes, tempo, key = await cheer_song.prepare(s, WHO)
        songs = await __import__("server_tools")._songs(s)
    played = [n for n in notes if n[0]]
    check("音符が起こせている", len(played) >= 10,
          "%s・%d 音" % (key, len(played)))
    check("音符に歌詞が乗っている", all(n[2] for n in played),
          "最初の 6 つ %s" % [n[2] for n in played[:6]])

    # ①リズム: 切れ目が音源の立ち上がりに乗っているか（でたらめ対照つき）
    path = cheer_song.local_audio(key)
    x = transcribe.load_audio(path)
    morae = cheer_song.moras("".join(songs.get(key, [])))
    kept = await asyncio.to_thread(
        lambda: segment_dp.build(path, morae)[2])
    ons = eval_onset.onsets(x)
    starts = np.array([a * 0.01 for a, _b in kept])
    got = eval_onset.score(starts, ons)
    lo = kept[0][0] * 0.01
    hi = kept[-1][1] * 0.01
    _even, rand = eval_onset.controls(lo, hi, len(kept), ons)
    check("リズムが音源に乗っている（でたらめ対照の %.0f%% 以下）"
          % (RHYTHM_MARGIN * 100),
          got <= rand * RHYTHM_MARGIN,
          "%.1fms（でたらめ %.1fms）" % (got, rand))

    print("その音符で歌わせる…")
    pcm, rate = await asyncio.to_thread(sing_vv.sung, key, notes, tempo)
    secs = len(pcm) / 2.0 / rate
    check("歌になっている", secs > 5, "%.1f 秒" % secs)

    # ②高さ: 楽譜どおりに歌えたか（移調は戻して比べる）
    shift = sing_vv.octave_shift([sing_vv.to_key(n[0]) for n in notes if n[0]])
    score = []
    for n in played:
        v = sing_vv.to_key(n[0])
        if not score or score[-1] != v:
            score.append(v)
    sung = contour(np.frombuffer(pcm, dtype="<i2").astype("float64") / 32768.0,
                   rate)
    back = [v - shift for v in sung]
    made = align_cost(back, score)
    check("楽譜どおりの高さで歌えている（%.1f 半音以内）" % MAX_SEMI,
          made <= MAX_SEMI,
          "%.2f 半音（楽譜 %d 音 / 歌 %d 音・移調 %+d）"
          % (made, len(score), len(sung), shift))
    orig = contour(x, transcribe.RATE)
    print("     参考: 採譜のずれ %.2f 半音・通しのずれ %.2f 半音（元 %d 音）"
          % (align_cost(score, orig), align_cost(back, orig), len(orig)))


# ⚠️ `test_sing_e2e` が補助関数（contour / align_cost）目当てでここを
# import する。読み込みだけで本体が走ると、向こうの event loop の
# 中で asyncio.run を呼ぶことになって落ちる
if __name__ == "__main__":
    asyncio.run(main())
    print("\n%d/%d" % (ok, ok + fail))
    sys.exit(1 if fail else 0)
