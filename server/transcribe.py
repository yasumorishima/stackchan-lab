"""歌の音源から音符を起こす（自動採譜）。

応援歌のメロディは、公式には歌詞しか出ていない。ドレミ表記を載せている個人
サイトは横浜の分を持っておらず、載っている所も画像だった。一方で、単旋律の
歌唱音源なら基本周波数を追えば音符に戻せる。他人の採譜を写すのではなく、
音そのものから起こす。

    ./.venv/bin/python transcribe.py <音源ファイル>

出力は sing.py がそのまま歌える形（[高さ, 16分音符いくつ分, 歌詞]）。
"""
import subprocess
import sys

import numpy as np

RATE = 16000
HOP = 0.01
WIN = 0.04
F_LO, F_HI = 80.0, 800.0
AC_GATE = 0.45          # 自己相関の山がこれ以下なら声と認めない
RMS_REL = 0.06          # いちばん大きいフレームに対する割合で無音を切る
NOTE_TOL = 0.7          # 同じ音とみなす高さの幅（半音）
MIN_NOTE = 0.06         # これより短い音は捨てる（秒）
GAP_UNVOICED = 0.05     # これだけ声が切れたら音の区切りとみなす（秒）
_STEPS = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def load_audio(path, rate=RATE):
    """ffmpeg で 16bit モノラルに直して読み込む。"""
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar", str(rate),
         "-f", "s16le", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
    if not out.stdout:
        raise RuntimeError("音を読めません: %s"
                           % out.stderr.decode("utf-8", "replace")[-200:])
    return np.frombuffer(out.stdout, dtype="<i2").astype("float64") / 32768.0


def f0_track(x, rate=RATE):
    """10ms ごとの高さ（半音・小数）。声でない所は NaN。"""
    n_win, n_hop = int(rate * WIN), int(rate * HOP)
    lo_lag, hi_lag = int(rate / F_HI), int(rate / F_LO)
    rms, semi = [], []
    for s in range(0, len(x) - n_win, n_hop):
        seg = x[s:s + n_win]
        rms.append(float(np.sqrt(np.mean(seg * seg))))
        seg = seg - seg.mean()
        ac = np.correlate(seg, seg, mode="full")[len(seg) - 1:]
        if ac[0] <= 0:
            semi.append(np.nan)
            continue
        ac = ac / ac[0]
        lag = int(np.argmax(ac[lo_lag:hi_lag])) + lo_lag
        if ac[lag] <= AC_GATE:
            semi.append(np.nan)
            continue
        semi.append(69.0 + 12.0 * np.log2((rate / lag) / 440.0))
    rms = np.array(rms)
    semi = np.array(semi)
    semi[rms < RMS_REL * rms.max()] = np.nan
    return semi


def fix_octaves(semi, k=15):
    """自己相関が 1 オクターブ間違える所を直す。

    周期の 2 倍・1/2 の所にも山が立つので、ときどき 12 半音ずれた値が出る。
    まわりの中央値から 12 半音ちょうど離れている値は、寄せて直す。
    """
    out = semi.copy()
    for i in range(len(semi)):
        if np.isnan(semi[i]):
            continue
        w = semi[max(0, i - k):i + k + 1]
        w = w[~np.isnan(w)]
        if len(w) < 5:
            continue
        med = float(np.median(w))
        for shift in (12.0, -12.0):
            if abs(semi[i] + shift - med) < abs(out[i] - med) - 6.0:
                out[i] = semi[i] + shift
    return out


def smooth(semi, k=5):
    """外れ値を落とす（中央値で均す）。"""
    out = semi.copy()
    for i in range(len(semi)):
        w = semi[max(0, i - k // 2):i + k // 2 + 1]
        w = w[~np.isnan(w)]
        out[i] = np.median(w) if len(w) else np.nan
    return out


def segment(semi):
    """同じ高さが続く所を 1 音にまとめて [(高さ, 始まり, 終わり)] を返す。"""
    notes = []
    cur, start = [], None
    silence = 0
    for i, s in enumerate(semi):
        if np.isnan(s):
            silence += 1
            if cur and silence * HOP >= GAP_UNVOICED:
                notes.append((float(np.median(cur)), start, i - silence))
                cur, start = [], None
            continue
        silence = 0
        if not cur:
            cur, start = [s], i
        elif abs(s - np.median(cur)) <= NOTE_TOL:
            cur.append(s)
        else:
            notes.append((float(np.median(cur)), start, i))
            cur, start = [s], i
    if cur:
        notes.append((float(np.median(cur)), start, len(semi)))
    return [(p, a, b) for p, a, b in notes if (b - a) * HOP >= MIN_NOTE]


def merge_same(notes):
    """同じ高さの音が続けて切れている所をつなぐ（息継ぎで割れた分）。"""
    out = []
    for pitch, a, b in notes:
        if out and abs(out[-1][0] - pitch) < 0.5 and (a - out[-1][2]) * HOP < 0.05:
            p0, a0, _b0 = out[-1]
            out[-1] = ((p0 + pitch) / 2.0, a0, b)
        else:
            out.append((pitch, a, b))
    return out


def best_unit(notes):
    """16分音符 1 つぶんの長さを、音の長さの割り切れ方から決める。

    細かい単位ほど「割り切れて」しまうので、ずれの小ささだけで選ぶと必ず
    いちばん細かい所に張り付く（実測でテンポ 242・188 と探索範囲の端に出た）。
    そこで**十分に割り切れる中でいちばん粗い単位**を採る。
    """
    lens = np.array([(b - a) * HOP for _p, a, b in notes])

    def err_of(u):
        k = lens / u
        return float(np.mean(np.abs(k - np.round(k))))   # 単位いくつ分のずれ

    grid = np.arange(0.20, 0.055, -0.001)
    errs = [(float(u), err_of(u)) for u in grid]
    for u, e in errs:
        if e <= 0.15:
            return u, e
    return min(errs, key=lambda t: t[1])


def to_name(semi):
    n = int(round(semi))
    return "%s%d" % (_STEPS[n % 12], n // 12 - 1)


def transcribe(path):
    """音源から (音符の並び, テンポ, 測り具合) を返す。"""
    x = load_audio(path)
    semi = smooth(fix_octaves(f0_track(x)))
    notes = merge_same(segment(semi))
    if not notes:
        raise RuntimeError("音符が見つかりません")
    unit, err = best_unit(notes)
    out = []
    prev_end = notes[0][1]
    for pitch, a, b in notes:
        gap = (a - prev_end) * HOP
        if gap >= unit * 0.75:
            out.append([None, max(1, int(round(gap / unit))), ""])
        out.append([to_name(pitch), max(1, int(round((b - a) * HOP / unit))),
                    ""])
        prev_end = b
    tempo = 60.0 / (unit * 4.0)
    return out, tempo, err


if __name__ == "__main__":
    notes, tempo, err = transcribe(sys.argv[1])
    total = sum(n[1] for n in notes)
    print("テンポ %.0f / 音 %d 個（休み込みで %d）/ 長さ %d（16分音符いくつ分）"
          " / 割り切れなさ %.3f"
          % (tempo, sum(1 for n in notes if n[0]), len(notes), total, err))
    print([[n[0], n[1]] for n in notes])
