"""採用する設定（前後に足す無音・声の大きさそろえ）を種違いで測って決める。

無音は伸ばすほど雑音に強いと出たが、25 文 1 回ぶんの差は運の可能性がある。
同じ条件を雑音の乱数 3 通りでまわして平均を見る。本番の経路ごとの A/B は
bench_stt_prod.py。

  ./.venv/bin/python bench_stt_pick.py
"""
import asyncio
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app
import opus_codec
import sherpa_onnx

RATE = 16000
FRAME_MS = 60
MODEL = os.path.expanduser(os.environ.get("SHERPA_DIR",
                                          "~/models/reazonspeech-k2-v2"))

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


def make_recognizer():
    return sherpa_onnx.OfflineRecognizer.from_transducer(
        encoder=os.path.join(MODEL, "encoder-epoch-99-avg-1.int8.onnx"),
        decoder=os.path.join(MODEL, "decoder-epoch-99-avg-1.int8.onnx"),
        joiner=os.path.join(MODEL, "joiner-epoch-99-avg-1.int8.onnx"),
        tokens=os.path.join(MODEL, "tokens.txt"),
        num_threads=4, sample_rate=RATE, feature_dim=80,
        decoding_method="greedy_search")


def recognize(rec, pcm, pad_s):
    audio = np.frombuffer(pcm, dtype="<i2").astype("float32") / 32768.0
    pad = np.zeros(int(RATE * pad_s), dtype="float32")
    audio = np.concatenate([pad, audio, pad])
    s = rec.create_stream()
    s.accept_waveform(RATE, audio)
    rec.decode_stream(s)
    return "".join(s.result.text.split())


def through_opus(pcm):
    enc = opus_codec.Encoder(RATE, 1, FRAME_MS)
    dec = opus_codec.Decoder(RATE, 1)
    out = b"".join(dec.decode(p) for p in enc.encode_stream(pcm))
    enc.close()
    dec.close()
    return out


def rms_of(x):
    return float(np.sqrt(np.mean(x * x))) or 1.0


def quieten(pcm, target_rms):
    x = np.frombuffer(pcm, dtype="<i2").astype("float64")
    x = x * (target_rms / rms_of(x))
    return np.clip(x, -32768, 32767).astype("<i2").tobytes()


def add_noise(pcm, snr_db, seed):
    x = np.frombuffer(pcm, dtype="<i2").astype("float64")
    rng = np.random.default_rng(seed)
    n = rms_of(x) / (10.0 ** (snr_db / 20.0))
    x = x + rng.normal(0.0, n, size=x.shape)
    return np.clip(x, -32768, 32767).astype("<i2").tobytes()


def normalize(pcm, target_rms):
    if not target_rms:
        return pcm
    x = np.frombuffer(pcm, dtype="<i2").astype("float64")
    x = np.clip(x * (target_rms / rms_of(x)), -32768, 32767)
    return x.astype("<i2").tobytes()


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


CONFIGS = [
    ("いまの設定 無音0.3/そのまま", 0.3, 0),
    ("無音0.8/音量3000", 0.8, 3000),
    ("無音1.2/音量3000", 1.2, 3000),
    ("無音0.8/音量6000", 0.8, 6000),
]


async def main():
    print("音声を作る…")
    raw = [(s, through_opus(await synth(s))) for s in SENTENCES]
    rec = make_recognizer()

    conds = [("そのまま", None), ("雑音SNR15", 15.0), ("雑音SNR10", 10.0)]
    for cname, snr in conds:
        print("\n===== %s =====" % cname)
        for label, pad_s, target in CONFIGS:
            cers, exacts = [], []
            t_all = audio = 0.0
            for seed in (0, 1000, 2000):
                errs = chars = exact = 0
                for i, (ref_text, pcm) in enumerate(raw):
                    fed = quieten(pcm, 900.0)
                    if snr is not None:
                        fed = add_noise(fed, snr, seed + i)
                    fed = normalize(fed, target)
                    t0 = time.monotonic()
                    got = recognize(rec, fed, pad_s)
                    t_all += time.monotonic() - t0
                    audio += len(fed) / 2.0 / RATE
                    ref = norm(ref_text)
                    e = levenshtein(ref, norm(got))
                    errs += e
                    chars += len(ref)
                    exact += (e == 0)
                cers.append(100.0 * errs / chars)
                exacts.append(exact)
                if snr is None:
                    break
            print("  %-24s CER %5.1f%% (%s)  完全一致 %s /25  rtf %.2f"
                  % (label, sum(cers) / len(cers),
                     " ".join("%.1f" % c for c in cers),
                     " ".join(str(x) for x in exacts), t_all / audio))


asyncio.run(main())
