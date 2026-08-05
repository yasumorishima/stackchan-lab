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
WIN = 0.06              # 窓（秒）
F_LO, F_HI = 80.0, 800.0
AC_GATE = 0.35          # 相関の山がこれ以下なら声と認めない
# 窓 0.06・判定 0.35 は閉ループで選んだ（2026-08-05）。採譜→歌唱→元音源と
# 照合したときの形のずれ: 宮﨑 2.64→1.49 半音・牧 2.14→2.00 半音、歌の
# 無音も 17.0→11.0% / 13.7→11.0% に減った（窓 0.04・判定 0.45 との比較）。
RMS_REL = 0.06          # いちばん大きいフレームに対する割合で無音を切る
NOTE_TOL = 0.7          # 同じ音とみなす高さの幅（半音）
VERSION = 5             # 起こし方を変えたら上げる（取ってある結果を捨てるため）
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
    """10ms ごとの高さ（半音・小数）。声でない所は NaN。

    🔴 相関は**重なった分だけで正規化する**（NCCF）。ずらすほど掛け合わせる
    サンプルが減るので、`ac/ac[0]` のままだと**低い音ほど山が低く出て**声と
    認められない。実測（2026-08-05・牧秀悟）: 45.4% が「声でない」とされ、その
    90% は音が鳴っていた（音量の中央値は声とした所と同じ 0.038）。正規化すると
    この取りこぼしが消える。
    """
    n_win, n_hop = int(rate * WIN), int(rate * HOP)
    lo_lag, hi_lag = int(rate / F_HI), int(rate / F_LO)
    lags = np.arange(lo_lag, hi_lag)
    rms, semi = [], []
    for s in range(0, len(x) - n_win, n_hop):
        seg = x[s:s + n_win]
        rms.append(float(np.sqrt(np.mean(seg * seg))))
        seg = seg - seg.mean()
        raw = np.correlate(seg, seg, mode="full")[len(seg) - 1:]
        if raw[0] <= 0:
            semi.append(np.nan)
            continue
        # 重なりの手前側・奥側それぞれの力（累積和で一度に出す）
        cs = np.concatenate(([0.0], np.cumsum(seg * seg)))
        head = cs[len(seg) - lags] - cs[0]
        tail = cs[len(seg)] - cs[lags]
        nccf = raw[lags] / np.sqrt(head * tail + 1e-12)
        k = int(np.argmax(nccf))
        if nccf[k] <= AC_GATE:
            semi.append(np.nan)
            continue
        semi.append(69.0 + 12.0 * np.log2((rate / lags[k]) / 440.0))
    rms = np.array(rms)
    semi = np.array(semi)
    semi[rms < RMS_REL * rms.max()] = np.nan
    return semi


OCTAVE_SPAN = 9.0        # 曲の中心からこれだけ離れたら 1 オクターブ間違い


def fix_octaves(semi):
    """自己相関が 1 オクターブ間違える所を直す。

    周期の 2 倍・1/2 の所にも山が立つので、ときどき 12 半音ずれた値が出る。
    **曲全体の中心**から 9 半音より離れた値は、12 半音ずつ寄せて中心に近づける
    （まわりの中央値と比べる作りにしたら、低い値が固まって出る所で直せなかった。
    実測: 牧秀悟の音源は中心 A4 なのに下位 5% が A1 だった）。9 半音までは動かさ
    ないので、1 オクターブ半に届く旋律はそのまま残る。
    """
    v = semi[~np.isnan(semi)]
    if len(v) < 10:
        return semi
    center = float(np.median(v))
    out = semi.copy()
    for i in range(len(out)):
        if np.isnan(out[i]):
            continue
        while out[i] - center > OCTAVE_SPAN:
            out[i] -= 12.0
        while center - out[i] > OCTAVE_SPAN:
            out[i] += 12.0
    return out


def smooth(semi, k=5):
    """外れ値を落とす（中央値で均す）。"""
    out = semi.copy()
    for i in range(len(semi)):
        w = semi[max(0, i - k // 2):i + k // 2 + 1]
        w = w[~np.isnan(w)]
        out[i] = np.median(w) if len(w) else np.nan
    return out


MAX_FILL = 0.12          # これより短い声の切れ目は埋める（秒）


def fill_gaps(semi, max_gap=MAX_FILL):
    """短い「声が出ていない」所を埋める。

    録音によっては歌っている最中も声が途切れて拾えず、1 つの音が細切れになる
    （実測: 牧秀悟の音源は 81 音のうち 58 が休みに割られた）。前後が同じくらいの
    高さなら、その間は歌い続けているとみなす。
    """
    out = semi.copy()
    n = int(max_gap / HOP)
    i = 0
    while i < len(out):
        if not np.isnan(out[i]):
            i += 1
            continue
        j = i
        while j < len(out) and np.isnan(out[j]):
            j += 1
        if 0 < i and j < len(out) and (j - i) <= n:
            if abs(out[i - 1] - out[j]) <= 2.0:
                out[i:j] = out[i - 1]
        i = j
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
    return absorb_short(notes)


def absorb_short(notes):
    """短すぎる区切りを**捨てずに**隣へ吸わせる。

    捨てると、その時間がまるごと休みになって歌に穴が開く。実測（2026-08-05・
    牧秀悟）: 音源は 25.2 秒ずっと鳴っているのに、量子化の段で 33% が休みに
    なり「休みが多すぎる」で歌えない曲に落ちていた。しゃくり上げや声のゆれで
    生まれる 60ms 未満の区切りは、前の音（無ければ次の音）に付けて時間を保つ。
    こうすると休みは**音源が本当に鳴っていない所だけ**になる。
    """
    out, pending = [], None
    for pitch, a, b in notes:
        if (b - a) * HOP < MIN_NOTE:
            if out:
                out[-1][2] = max(out[-1][2], b)
            elif pending is None:
                pending = a
            continue
        if pending is not None:
            a, pending = min(a, pending), None
        out.append([pitch, a, b])
    return [(p, a, b) for p, a, b in out]


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


MIN_REST_SEC = 0.25      # これより短い休みは前の音の伸ばしにする
MIN_NOTE_SEC = 0.15      # 歌う音はこれより短くしない（歌詞が 1 文字入る長さ）


def tidy(notes, unit):
    """細切れをまとめる。

    録音から起こすと、息継ぎや子音の所で音が切れて短い休みだらけになる
    （実測: 牧秀悟の応援歌は 74 音に対して休みが 50 個・全体の 50% が休みで、
    音の半分近くが 100ms 未満だった）。そのまま歌わせると、歌ではなく短い音の
    連打になる。短い休みは前の音を伸ばして吸収し、短すぎる音は伸ばす。
    """
    min_rest = max(1, int(round(MIN_REST_SEC / unit)))
    min_note = max(1, int(round(MIN_NOTE_SEC / unit)))
    out = []
    for pitch, length in [(n[0], n[1]) for n in notes]:
        if pitch is None and length < min_rest and out:
            out[-1][1] += length
            continue
        if out and pitch is not None and out[-1][0] == pitch:
            out[-1][1] += length
            continue
        out.append([pitch, length, ""])
    # 🔴 短い音を「伸ばす」と**その分だけ曲が長くなり、拍がずれる**。
    # 足りない長さは**次の音から借りる**（合計の長さを変えない）。借りられ
    # なければそのまま短く歌う（無理に伸ばすより崩れない）
    for i, n in enumerate(out):
        if n[0] is None or n[1] >= min_note:
            continue
        need = min_note - n[1]
        for j in range(i + 1, len(out)):
            spare = out[j][1] - (1 if out[j][0] is None else min_note)
            if spare <= 0:
                continue
            take = min(need, spare)
            out[j][1] -= take
            n[1] += take
            need -= take
            if need <= 0:
                break
    return out


def to_name(semi):
    n = int(round(semi))
    return "%s%d" % (_STEPS[n % 12], n // 12 - 1)


def transcribe(path):
    """音源から (音符の並び, テンポ, 測り具合) を返す。"""
    x = load_audio(path)
    semi = fill_gaps(smooth(fix_octaves(f0_track(x))))
    notes = merge_same(segment(semi))
    if not notes:
        raise RuntimeError("音符が見つかりません")
    # 🔴 **テンポの格子に丸めない**（2026-08-05）。格子は推定でしかなく、同じ曲で
    # 106 とも 176 とも出る。丸めた瞬間に音の長さが全部その誤差ぶん狂い、
    # 実測で**音の立ち上がりが元音源から 662〜912ms ずれた**（user 様「リズム
    # 悪すぎ」）。歌わせるのに楽譜の見た目は要らないので、**測った長さ（10ms
    # 刻み）をそのまま渡す**。単位を 10ms にすると tempo は 1500 になるが、
    # これは「1 単位 = 10ms」を表すだけの数で、曲の速さではない。
    _unit, err = best_unit(notes)                 # 記録用（丸めには使わない）
    unit = HOP
    out = []
    prev_end = notes[0][1]
    for pitch, a, b in notes:
        gap = a - prev_end
        if gap >= 2:                              # 20ms 以上あいたら休み
            out.append([None, int(gap), ""])
        out.append([to_name(pitch), max(1, int(b - a)), ""])
        prev_end = b
    tempo = 60.0 / (unit * 4.0)
    return tidy(out, unit), tempo, err


if __name__ == "__main__":
    notes, tempo, err = transcribe(sys.argv[1])
    total = sum(n[1] for n in notes)
    print("テンポ %.0f / 音 %d 個（休み込みで %d）/ 長さ %d（16分音符いくつ分）"
          " / 割り切れなさ %.3f"
          % (tempo, sum(1 for n in notes if n[0]), len(notes), total, err))
    print([[n[0], n[1]] for n in notes])
