"""歌詞を手掛かりに、モーラが実際に歌われている時刻を割り出す。

これまでは音だけを眺めて切れ目を探していた（高さの変化・立ち上がり・拍）。
どれも頭打ちだった。こちらは**歌詞という答え**を持っているので、それを使う:

  1. 歌詞を Open JTalk に読ませる。`-ot` の出力に**音素ごとの時刻**が出るので、
     参照音声の中で各モーラが何秒何分かが厳密に分かる
  2. 参照音声と本物の音源を MFCC で対応づける（DTW）
  3. 参照の各モーラ開始時刻を、本物の側の時刻へ写す
  4. その区間の長さを音符の長さ、その区間の高さの中央値を音符の高さにする

読み（音素）が同じもの同士を突き合わせるので、話し声と歌声で音色が違っても
対応づけは効く。
"""
import os
import re
import subprocess
import sys
import tempfile
import wave

import numpy as np

sys.path.insert(0, "/home/yasu/stackchan-server")
os.chdir("/home/yasu/stackchan-server")

import transcribe  # noqa: E402

OJT = "open_jtalk"
DIC = "/var/lib/mecab/dic/open-jtalk/naist-jdic"
VOICE = ("/usr/share/hts-voice/nitech-jp-atr503-m001/"
         "nitech_jp_atr503_m001.htsvoice")
RATE = 16000
HOP = 0.01
WIN = 0.025
N_MEL, N_MFCC = 26, 13
VOWELS = set("aiueoAIUEO")


def synth_with_labels(text):
    """読ませた音声と、音素ごとの (開始秒, 終了秒, 音素) を返す。"""
    with tempfile.TemporaryDirectory() as tmp:
        wav = os.path.join(tmp, "o.wav")
        trace = os.path.join(tmp, "t.txt")
        r = subprocess.run([OJT, "-x", DIC, "-m", VOICE, "-ow", wav,
                            "-ot", trace, "-r", "1.0"],
                           input=text.encode("utf-8"),
                           capture_output=True, timeout=120)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.decode("utf-8", "replace")[:200])
        with wave.open(wav, "rb") as w:
            sr = w.getframerate()
            x = np.frombuffer(w.readframes(w.getnframes()),
                              dtype="<i2").astype("float64") / 32768.0
        lab = []
        for line in open(trace, encoding="utf-8", errors="replace"):
            # 貪欲にすると後ろのフィールドの `-` まで食う（実測で 0 件になった）
            m = re.match(r"^(\d+) (\d+) [^\s^]+\^[^-]+-([^+]+)\+", line)
            if m:
                lab.append((int(m.group(1)) / 1e7, int(m.group(2)) / 1e7,
                            m.group(3)))
    if sr != RATE:
        x = np.interp(np.arange(0, len(x), sr / RATE),
                      np.arange(len(x)), x)
    return x, lab


def mora_starts(lab):
    """母音（と N・cl）ごとに 1 モーラと数え、その開始秒を返す。"""
    out = []
    for k, (a, _b, p) in enumerate(lab):
        if p in ("sil", "pau"):
            continue
        if p in VOWELS or p in ("N", "cl"):
            # 直前が子音ならそこがモーラの始まり
            start = a
            if k > 0 and lab[k - 1][2] not in ("sil", "pau") \
                    and lab[k - 1][2] not in VOWELS \
                    and lab[k - 1][2] not in ("N", "cl"):
                start = lab[k - 1][0]
            out.append(start)
    return out


def mfcc(x, rate=RATE):
    n_win, n_hop = int(rate * WIN), int(rate * HOP)
    n_fft = 1
    while n_fft < n_win:
        n_fft *= 2
    win = np.hamming(n_win)
    # メルフィルタバンク
    def hz2mel(f):
        return 2595.0 * np.log10(1.0 + f / 700.0)

    def mel2hz(m):
        return 700.0 * (10.0 ** (m / 2595.0) - 1.0)
    lo, hi = hz2mel(80.0), hz2mel(rate / 2.0)
    pts = mel2hz(np.linspace(lo, hi, N_MEL + 2))
    bins = np.floor((n_fft + 1) * pts / rate).astype(int)
    fb = np.zeros((N_MEL, n_fft // 2 + 1))
    for m in range(1, N_MEL + 1):
        a, b, c = bins[m - 1], bins[m], bins[m + 1]
        if c <= a:
            continue
        for k in range(a, b):
            fb[m - 1, k] = (k - a) / max(b - a, 1)
        for k in range(b, c):
            fb[m - 1, k] = (c - k) / max(c - b, 1)
    out = []
    for s in range(0, len(x) - n_win, n_hop):
        spec = np.abs(np.fft.rfft(x[s:s + n_win] * win, n_fft)) ** 2
        e = np.log(fb.dot(spec) + 1e-10)
        # 0 番目は全体の音量。合唱と単声で大きく違うので使わない
        c = np.fft.rfft(e).real[1:N_MFCC + 1]
        out.append(c)
    m = np.array(out)
    m = (m - m.mean(axis=0)) / (m.std(axis=0) + 1e-9)   # 音色の差を均す
    return m


def dtw_map(a, b, free_ends=False):
    """a の各フレームが b のどのフレームに当たるか。

    `free_ends` のとき、b の**どこから始まってどこで終わってもよい**。
    本物の音源には歌詞に無い前奏・後奏が付いているので、端を固定すると
    そこを無理に歌詞へ割り当てて全体がずれる。
    """
    n, m = len(a), len(b)
    band = max(200, int(abs(n - m)) + 200)
    d = np.full((n + 1, m + 1), np.inf, dtype=np.float32)
    if free_ends:
        d[0, :] = 0.0          # b のどこから始めてもよい
    else:
        d[0, 0] = 0.0
    scale = m / float(n)
    for i in range(1, n + 1):
        c = int(i * scale)
        lo, hi = max(1, c - band), min(m, c + band)
        diff = a[i - 1] - b[lo - 1:hi]
        cost = np.sqrt((diff * diff).sum(axis=1))
        row, prow = d[i], d[i - 1]
        for k, j in enumerate(range(lo, hi + 1)):
            row[j] = cost[k] + min(prow[j - 1], prow[j], row[j - 1])
    if free_ends:
        j = int(np.argmin(d[n, 1:])) + 1      # b のどこで終わってもよい
    else:
        j = m
    i = n
    out = np.zeros(n, dtype=int)
    while i > 0 and j > 0:
        out[i - 1] = j - 1
        step = min(d[i - 1, j - 1], d[i - 1, j], d[i, j - 1])
        if step == d[i - 1, j - 1]:
            i, j = i - 1, j - 1
        elif step == d[i - 1, j]:
            i -= 1
        else:
            j -= 1
    return out


def build(path, text, morae):
    """(音符, tempo=1500) を返す。長さは 10ms 単位。"""
    ref, lab = synth_with_labels(text)
    starts = mora_starts(lab)
    if len(starts) < 2:
        raise RuntimeError("参照から音節が取れない")
    x = transcribe.load_audio(path)
    warp = dtw_map(mfcc(ref), mfcc(x))
    semi = transcribe.fill_gaps(transcribe.smooth(
        transcribe.fix_octaves(transcribe.f0_track(x))))
    n = min(len(starts), len(morae))
    bounds = []
    for k in range(n):
        f = min(int(starts[k] / HOP), len(warp) - 1)
        bounds.append(int(warp[f]))
    bounds.append(len(semi))
    notes = []
    for k in range(n):
        a, b = bounds[k], max(bounds[k] + 2, bounds[k + 1])
        b = min(b, len(semi))
        if b - a < 2:
            continue
        seg = semi[a:b]
        seg = seg[~np.isnan(seg)]
        if len(seg) < 1:
            notes.append([None, int(b - a), ""])
            continue
        notes.append([transcribe.to_name(float(np.median(seg))),
                      int(b - a), morae[k]])
    return notes, 1500.0, len(starts)
