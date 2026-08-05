"""採譜のやり方を、リズムの物差し（rhythm_eval）で横並びに測る。

⚠️ ここが前回いちばん効いた反省: **正解の無い実曲だけで比べると、どの方式も
横並びに悪く見えて判断できない**。自分で書いた楽譜を歌わせたものを「音源」と
みなして起こし直す（正解つきの曲）と、初めて優劣が出る。

⚠️ もう 1 つの反省: 音源ぜんぶと比べていたので、**歌詞が音源の一部しか覆わない
曲では正しい答えを不正解と採点していた**。範囲を決める方式には、その範囲を
返してもらい、**選んだ範囲と比べる**。

使い方:
  --controlled [--contrast]   正解つきの曲で測る（--contrast は長短の差が大きい版）
  --real [曲名 ...]           実曲で測る
"""
import asyncio
import os
import sys
import wave

import numpy as np

sys.path.insert(0, "/home/yasu/stackchan-server")
os.chdir("/home/yasu/stackchan-server")

import rhythm_eval as re_  # noqa: E402
import sing_vv  # noqa: E402
import transcribe  # noqa: E402

HOP = 0.01
TMP = "/tmp/eval_rhythm"

# 正解つきの曲。長さは 10ms いくつ分。
BASE = [("E4", 30), ("E4", 30), ("G4", 30), ("G4", 30),
        ("A4", 30), ("A4", 30), ("G4", 60),
        ("F4", 30), ("F4", 30), ("E4", 30), ("E4", 30),
        ("D4", 30), ("D4", 30), ("C4", 60),
        ("E4", 30), ("G4", 30), ("A4", 30), ("G4", 30),
        ("F4", 30), ("E4", 30), ("D4", 30), ("E4", 30),
        ("G4", 30), ("C5", 60)]
# 長短の差が大きい版（200〜800ms）。長さの縛りをこれで決める
CONTRAST = [20, 20, 80, 40, 20, 20, 80, 40, 20, 80, 20, 40,
            20, 20, 80, 40, 80, 20, 20, 40, 20, 80, 20, 80]
LYRIC = "かっとばせみやざきホームランをうてよそれゆけみやざきかっとばせ"


def _score(contrast=False):
    morae = list(sing_vv.to_morae(LYRIC)[0])[:len(BASE)]
    while len(morae) < len(BASE):
        morae.append("ラ")
    notes = []
    for k, (pitch, dur) in enumerate(BASE):
        notes.append([pitch, CONTRAST[k] if contrast else dur, morae[k]])
    return notes, [m for m in morae]


def _write(pcm, rate, path):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


def _as16k(pcm, rate):
    x = np.frombuffer(pcm, dtype="<i2").astype("float64") / 32768.0
    if rate != re_.RATE:
        x = np.interp(np.arange(0, len(x), rate / float(re_.RATE)),
                      np.arange(len(x)), x)
    return x


def _render(notes, tempo):
    pcm, rate = sing_vv.sing_sync(notes, tempo)
    return _as16k(pcm, rate), pcm, rate


def _methods():
    import align_sung
    import combo
    import segment_dp
    import segment_span
    return [("DTW（歌詞合わせ）", align_sung.build),
            ("DP（モーラ数）", segment_dp.build),
            ("DP＋DTW", combo.build),
            ("範囲も決める DP", segment_span.build)]


def _measure(name, build, path, morae, src):
    try:
        out = build(path, morae)
    except Exception as e:                       # noqa: BLE001
        return name, None, None, "%s: %s" % (type(e).__name__, str(e)[:60])
    notes, tempo = out[0], out[1]
    kept = out[2] if len(out) > 2 else None
    span = (kept[0][0] * HOP, kept[-1][1] * HOP) if kept else None
    try:
        sung, _pcm, _rate = _render(notes, tempo)
    except Exception as e:                       # noqa: BLE001
        return name, None, None, "歌えず: %s" % str(e)[:60]
    ref = src
    if span:
        lo, hi = span
        ref = src[int(lo * re_.RATE):int(hi * re_.RATE)]
    try:
        dev, _n = re_.deviation(sung, ref)
    except Exception as e:                       # noqa: BLE001
        return name, None, notes, "測れず: %s" % str(e)[:60]
    return name, dev, notes, None


def _pitch_hits(notes, truth):
    if len(notes) != len(truth):
        return None
    hit = 0
    for got, (want, _d) in zip(notes, truth):
        if got[0] == want:
            hit += 1
    return hit


def controlled(contrast=False):
    os.makedirs(TMP, exist_ok=True)
    notes, morae = _score(contrast)
    src_f, pcm, rate = _render(notes, 1500.0)
    path = os.path.join(TMP, "controlled%s.wav" % ("_c" if contrast else ""))
    _write(pcm, rate, path)
    total = sum(n[1] for n in notes) * HOP
    print("正解つきの曲%s: %d 音・%.1f 秒（%s）"
          % ("（長短の差が大きい版）" if contrast else "",
             len(notes), total, path))
    print("  %-18s %8s %10s" % ("やり方", "ずれ", "高さ一致"))
    for name, build in _methods():
        n, dev, got, err = _measure(name, build, path, morae, src_f)
        if err:
            print("  %-18s  %s" % (n, err))
            continue
        hits = _pitch_hits(got, [(p, d) for p, d in
                                 [(x[0], x[1]) for x in notes]])
        print("  %-18s %6.0fms %8s"
              % (n, dev, "%d/%d" % (hits, len(notes))
                 if hits is not None else "—"))


async def real(names):
    import aiohttp

    import cheer_song
    import server_tools
    async with aiohttp.ClientSession() as s:
        songs = await server_tools._songs(s)
        if not names:
            names = ["宮﨑敏郎", "牧秀悟", "関根大気", "戸柱恭孝", "佐野恵太"]
        print("  %-10s %-18s %8s" % ("曲", "やり方", "ずれ"))
        for want in names:
            key = await asyncio.to_thread(server_tools._find_player,
                                          songs, want)
            text = "".join(songs.get(key, [])) if key else ""
            path = cheer_song.local_audio(key) if key else None
            if not text or not path:
                print("  %-10s  歌詞か音源が無い" % want)
                continue
            morae = cheer_song.moras(text)
            src = transcribe.load_audio(path)
            for name, build in _methods():
                n, dev, _got, err = _measure(name, build, path, morae, src)
                print("  %-10s %-18s %s"
                      % (key, n, err if err else "%6.0fms" % dev))


def main(argv):
    if "--controlled" in argv:
        controlled("--contrast" in argv)
        return 0
    if "--real" in argv:
        rest = [a for a in argv[argv.index("--real") + 1:]
                if not a.startswith("--")]
        asyncio.run(real(rest))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
