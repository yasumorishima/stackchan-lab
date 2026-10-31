import asyncio, glob, sys, wave
import numpy as np
sys.path.insert(0, "/home/yasu/stackchan-server")
import app

def stats(pcm, label, rate):
    a = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
    if not len(a):
        print(label, "empty"); return
    frame = int(rate * 0.02)
    n = len(a) // frame
    f = a[: n * frame].reshape(n, frame)
    rms = np.sqrt((f ** 2).mean(axis=1))
    voiced = rms[rms > 200]
    print("%-28s 全体rms %6.0f  有声rms %6.0f  peak %6.0f  %5.1f 秒" % (
        label, np.sqrt((a ** 2).mean()), voiced.mean() if len(voiced) else 0,
        np.abs(a).max(), len(a) / rate))

for p in sorted(glob.glob("/home/yasu/stackchan-server/cache/songs/*sung*.wav"))[:4]:
    with wave.open(p, "rb") as w:
        stats(w.readframes(w.getnframes()), p.rsplit("/", 1)[-1][:26], w.getframerate())

async def main():
    for text in ["こんにちは、今日の横浜は晴れです。", "みやざきとしろうの応援歌です。さあふりぬけみやざき、きはくあふれるパワーで。"]:
        pcm, pace = await app._openjtalk_once(text)
        stats(pcm, "OJT: " + text[:12], app.DOWN_RATE)

asyncio.run(main())
