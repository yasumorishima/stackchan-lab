"""認識器に渡す前の下ごしらえ（切り出し・音量そろえ）の試験。

実機は 1 語の返事にも十数秒のバッファを渡してくる。黙っている所を落として
から渡すかどうかで CER が倍近く違ったので（docs/progress.md）、その下ごしらえ
が壊れていないかをここで見る。認識器は読まないので速い。

  ./.venv/bin/python test_stt_front.py
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import local_stt

RATE = 16000
ok = fail = 0


def check(name, cond, got=""):
    global ok, fail
    if cond:
        ok += 1
        print("OK %s %s" % (name, got))
    else:
        fail += 1
        print("NG %s %s" % (name, got))


def speech(seconds=1.0, amp=0.2, seed=0):
    """声の代わり（振幅のある波）。"""
    rng = np.random.default_rng(seed)
    n = int(RATE * seconds)
    t = np.arange(n) / RATE
    x = np.sin(2 * np.pi * 220 * t) * amp
    return (x + rng.normal(0, amp * 0.1, n)).astype("float32")


def silence(seconds, amp=0.0, seed=1):
    rng = np.random.default_rng(seed)
    n = int(RATE * seconds)
    return rng.normal(0, amp, n).astype("float32") if amp else np.zeros(n, "float32")


# 切り出し
core = speech(1.0)
buf = np.concatenate([silence(6.0, 0.002), core, silence(6.0, 0.002)])
cut = local_stt._trim_to_speech(buf, RATE, 0.4)
check("前後の無音を落とす", 1.6 * RATE <= len(cut) <= 2.1 * RATE,
      "%.2f秒 -> %.2f秒" % (len(buf) / RATE, len(cut) / RATE))

check("声そのものは削らない",
      len(local_stt._trim_to_speech(core, RATE, 0.4)) == len(core))

allnoise = silence(3.0, 0.002)
check("全部が雑音なら何もしない",
      len(local_stt._trim_to_speech(allnoise, RATE, 0.4)) == len(allnoise))

check("無音だけでも落ちない",
      len(local_stt._trim_to_speech(np.zeros(RATE, "float32"), RATE, 0.4))
      == RATE)

check("短すぎるバッファはそのまま",
      len(local_stt._trim_to_speech(np.zeros(100, "float32"), RATE, 0.4))
      == 100)

# 突発音が混じった場合（査読の指摘。古い決め方＝「いちばん大きいフレームの
# 2 割」だと、咳やドアの音 1 つで本当の声が下に沈んで切り落とされた）
spike = np.zeros(int(RATE * 0.06), "float32")
spike[:] = 0.9
quiet_voice = speech(1.0, amp=0.03, seed=7)
mixed = np.concatenate([silence(3.0, 0.002), spike, silence(3.0, 0.002),
                        quiet_voice, silence(3.0, 0.002)])

f_rms, _step = local_stt._frame_rms(mixed, RATE)
voice_rms = float(np.sqrt(np.mean(quiet_voice * quiet_voice)))
check("古い決め方なら声が沈んでいた（バグの再現）",
      voice_rms < float(f_rms.max()) * 0.2,
      "声 %.4f < 旧しきい値 %.4f" % (voice_rms, float(f_rms.max()) * 0.2))

cut = local_stt._trim_to_speech(mixed, RATE, 0.4)
check("轟音が混じっても声は残す", len(cut) >= 1.0 * RATE,
      "%.2f秒 -> %.2f秒" % (len(mixed) / RATE, len(cut) / RATE))

lv_mixed = local_stt._speech_level(mixed, RATE)
check("轟音に引っぱられて音量を測り違えない", lv_mixed < 4000.0,
      "%.0f（声そのものは %.0f）" % (lv_mixed, voice_rms * 32768.0))

# 音量そろえ（黙っていた長さで倍率が変わらないこと）
lv_tight = local_stt._speech_level(core, RATE)
lv_long = local_stt._speech_level(buf, RATE)
check("黙っていた長さで音量が変わらない",
      abs(lv_tight - lv_long) / lv_tight < 0.1,
      "%.0f vs %.0f" % (lv_tight, lv_long))

lv_zero = local_stt._speech_level(np.zeros(RATE, "float32"), RATE)
check("無音の音量は 0", lv_zero == 0.0)

# 既定値（実測で決めた値から勝手にずれていないか）
check("既定 無音0.8秒", local_stt.SHERPA_PAD == 0.8, str(local_stt.SHERPA_PAD))
check("既定 音量3000", local_stt.SHERPA_NORM_RMS == 3000.0,
      str(local_stt.SHERPA_NORM_RMS))
check("既定 切り出し0.4秒", local_stt.SHERPA_TRIM == 0.4,
      str(local_stt.SHERPA_TRIM))


# 実機のバッファは十数秒あり、前の方に生活音が入っていることがある。答える
# べきなのは VAD が終わりを見つけた**最後のかたまり**なので、そこだけ渡す
# （2026-08-08 user「反応が遅い」→ 渡す長さを短くした）
def _burst(sec, amp, freq=300.0):
    t = np.arange(int(RATE * sec)) / RATE
    return (amp * np.sin(2 * np.pi * freq * t)).astype("float32")


def _silence(sec):
    return np.zeros(int(RATE * sec), dtype="float32")


_buf = np.concatenate([_silence(0.5), _burst(0.6, 0.30), _silence(4.0),
                       _burst(1.2, 0.35), _silence(0.3)])
_kept = local_stt._trim_to_speech(_buf, RATE, 0.4)
check("前の生活音を捨てて最後の発話だけ渡す",
      1.6 <= len(_kept) / RATE <= 2.6, "%.2f 秒（元 %.2f 秒）"
      % (len(_kept) / RATE, len(_buf) / RATE))

_one = np.concatenate([_silence(0.5), _burst(0.5, 0.30), _silence(0.3),
                       _burst(0.5, 0.30), _silence(0.5)])
_kept1 = local_stt._trim_to_speech(_one, RATE, 0.4)
check("短い間（0.3秒）は同じ発話として残す",
      1.8 <= len(_kept1) / RATE <= 2.4, "%.2f 秒（元 %.2f 秒）"
      % (len(_kept1) / RATE, len(_one) / RATE))

print("\n%d/%d" % (ok, ok + fail))
sys.exit(1 if fail else 0)
