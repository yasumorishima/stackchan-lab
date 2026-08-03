"""本番の経路（local_stt.transcribe）で、下ごしらえの効きを A/B する。

実機のバッファに近い形（前後に何秒も雑音だけが続く）と、声の大きさ違い
（小さい声・ふつう・大きい声）で測る。査読で出た「倍率に下限が無い」
「小さい声で振り切れる」の 2 点は、良し悪しを決めていないのでここで測る。

  ./.venv/bin/python bench_stt_prod.py
"""
import asyncio
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app
import local_stt
import opus_codec

RATE = 16000
FRAME_MS = 60

SENTENCES = [
    "ベイスターズの速報を教えて。",
    "今日の出場選手登録と抹消を教えて。",
    "牧秀悟の応援歌を教えて。",
    "燃油サーチャージはいくらですか。",
    "ドバイの渡航情報を教えて。",
    "ナスダックと日経平均を教えて。",
    "一週間の天気を教えて。",
    "ドル円は今いくら。",
    "ビットコインの値段を教えて。",
    "熱中症の危険度を教えて。",
    "都営地下鉄は遅れてる。",
    "月齢と日の出の時刻を教えて。",
    "気象庁の警報は出ている。",
    "台風は近づいてる。",
    "横浜スタジアムの試合はどうなった。",
    "こんにちは、スタックちゃんです。",
    "音楽をかけてください。",
    "ありがとう、また後でね。",
    "この部屋の温度は何度くらい。",
    "十分後にタイマーをセットして。",
    "横浜駅までの行き方を知りたい。",
    "おはよう、今日もよろしくね。",
    "その話はもう少し詳しく教えて。",
    "電気を消してくれる。",
    "今の時間は何時ですか。",
]


def through_opus(pcm):
    enc = opus_codec.Encoder(RATE, 1, FRAME_MS)
    dec = opus_codec.Decoder(RATE, 1)
    out = b"".join(dec.decode(p) for p in enc.encode_stream(pcm))
    enc.close()
    dec.close()
    return out


def rms_of(x):
    return float(np.sqrt(np.mean(x * x))) or 1.0


def build(pcm, snr_db, seed, silence_s, voice_rms):
    """実機なみのバッファを作る。雑音は前後の黙っている所にも置く。"""
    x = np.frombuffer(pcm, dtype="<i2").astype("float64")
    x = x * (voice_rms / rms_of(x))
    if silence_s:
        pad = np.zeros(int(RATE * silence_s))
        x = np.concatenate([pad, x, pad])
    if snr_db is not None:
        rng = np.random.default_rng(seed)
        n = voice_rms / (10.0 ** (snr_db / 20.0))
        x = x + rng.normal(0.0, n, size=x.shape)
    return np.clip(x, -32768, 32767).astype("<i2").tobytes()


def norm(s):
    return "".join(c for c in s if c not in "、。！？,.!? ー")


def levenshtein(a, b):
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


async def synth(text):
    pcm = await app.openjtalk_tts(text)
    return app.resample_linear(pcm, app.DOWN_RATE, RATE)


# 名前, 無音[s], 音量そろえ先, 切り出し[s], 倍率下限, 振り切れ防止
SETTINGS = [
    ("いままで", 0.3, 0.0, 0.0, 0.0, 0.0),
    ("直した後", 0.8, 3000.0, 0.4, 0.0, 0.0),
    ("＋倍率下限1", 0.8, 3000.0, 0.4, 1.0, 0.0),
    ("＋振り切れ防止", 0.8, 3000.0, 0.4, 0.0, 0.95),
]


async def main():
    print("音声を作る…")
    raw = [(s, through_opus(await synth(s))) for s in SENTENCES]
    local_stt.warmup("sherpa")

    conds = [("きれい・そのまま", None, 0.0, 900.0),
             ("雑音SNR15・前後に6秒", 15.0, 6.0, 900.0),
             ("雑音SNR10・前後に6秒", 10.0, 6.0, 900.0),
             ("大きい声(rms6000)・SNR15・前後に6秒", 15.0, 6.0, 6000.0),
             ("小さい声(rms300)・SNR15・前後に6秒", 15.0, 6.0, 300.0)]
    for cname, snr, sil, voice in conds:
        print("\n===== %s =====" % cname)
        for label, pad_s, target, trim, min_gain, clip in SETTINGS:
            local_stt.SHERPA_PAD = pad_s
            local_stt.SHERPA_NORM_RMS = target
            local_stt.SHERPA_TRIM = trim
            local_stt.SHERPA_NORM_MIN_GAIN = min_gain
            local_stt.SHERPA_NORM_CLIP = clip
            errs = chars = exact = 0
            t_all = audio = 0.0
            for i, (ref_text, pcm) in enumerate(raw):
                fed = build(pcm, snr, i, sil, voice)
                t0 = time.monotonic()
                got = await local_stt.transcribe("sherpa", fed, RATE)
                t_all += time.monotonic() - t0
                audio += len(fed) / 2.0 / RATE
                ref = norm(ref_text)
                e = levenshtein(ref, norm(got))
                errs += e
                chars += len(ref)
                exact += (e == 0)
            print("  %-14s CER %5.1f%%  完全一致 %2d/%d  認識 %.1f秒/音声 %.0f秒"
                  % (label, 100.0 * errs / chars, exact, len(raw),
                     t_all, audio))


asyncio.run(main())
