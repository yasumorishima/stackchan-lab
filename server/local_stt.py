"""ローカル STT バックエンド（vosk / faster-whisper）。

さくらの AI Engine は STT が月 50 回しかないので、常用は RPi5 上のローカル認識に寄せる。
どちらもモデルは初回のみダウンロードし、以後はオフラインで動く。

  STT_BACKEND=vosk     VOSK_MODEL=~/models/vosk-model-small-ja-0.22
  STT_BACKEND=whisper  WHISPER_MODEL=small  WHISPER_COMPUTE=int8

いずれもブロッキング API なので、呼び出しは asyncio.to_thread 経由にしてある。
"""
import asyncio
import json
import logging
import os
import threading

log = logging.getLogger("stackchan.stt")

VOSK_MODEL = os.environ.get("VOSK_MODEL", os.path.expanduser("~/models/vosk-model-small-ja-0.22"))
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small")
WHISPER_COMPUTE = os.environ.get("WHISPER_COMPUTE", "int8")
WHISPER_THREADS = int(os.environ.get("WHISPER_THREADS", "4"))

_lock = threading.Lock()
_vosk_model = None
_whisper_model = None


def _strip_spaces(text: str) -> str:
    # vosk の日本語モデルは形態素の間に半角空白を入れて返すので畳む
    return "".join(text.split())


def _load_vosk():
    global _vosk_model
    with _lock:
        if _vosk_model is None:
            import vosk
            vosk.SetLogLevel(-1)
            if not os.path.isdir(VOSK_MODEL):
                raise RuntimeError("vosk model not found: " + VOSK_MODEL)
            log.info("loading vosk model %s", VOSK_MODEL)
            _vosk_model = vosk.Model(VOSK_MODEL)
    return _vosk_model


def _load_whisper():
    global _whisper_model
    with _lock:
        if _whisper_model is None:
            from faster_whisper import WhisperModel
            log.info("loading faster-whisper %s (%s)", WHISPER_MODEL, WHISPER_COMPUTE)
            _whisper_model = WhisperModel(WHISPER_MODEL, device="cpu",
                                          compute_type=WHISPER_COMPUTE,
                                          cpu_threads=WHISPER_THREADS)
    return _whisper_model


def _vosk_sync(pcm: bytes, rate: int) -> str:
    import vosk
    model = _load_vosk()
    rec = vosk.KaldiRecognizer(model, float(rate))
    rec.SetWords(False)
    step = rate * 2  # 1 秒ずつ
    for i in range(0, len(pcm), step):
        rec.AcceptWaveform(pcm[i:i + step])
    result = json.loads(rec.FinalResult())
    return _strip_spaces(result.get("text", ""))


def _whisper_sync(pcm: bytes, rate: int) -> str:
    import numpy as np
    model = _load_whisper()
    audio = np.frombuffer(pcm, dtype="<i2").astype("float32") / 32768.0
    if rate != 16000:
        # faster-whisper は 16k 前提。線形補間で合わせる
        n = int(len(audio) * 16000 / rate)
        audio = np.interp(np.linspace(0, len(audio) - 1, n),
                          np.arange(len(audio)), audio).astype("float32")
    segments, _info = model.transcribe(audio, language="ja", beam_size=1,
                                       vad_filter=True, condition_on_previous_text=False)
    return "".join(s.text for s in segments).strip()


def warmup(backend: str):
    """起動時にモデルを読んでおく（初回発話の待ち時間をなくす）。"""
    if backend == "vosk":
        _load_vosk()
    elif backend == "whisper":
        _load_whisper()
    elif backend == "sherpa":
        _load_sherpa()


async def transcribe(backend: str, pcm: bytes, rate: int) -> str:
    if backend == "vosk":
        return await asyncio.to_thread(_vosk_sync, pcm, rate)
    if backend == "whisper":
        return await asyncio.to_thread(_whisper_sync, pcm, rate)
    if backend == "sherpa":
        return await asyncio.to_thread(_sherpa_sync, pcm, rate)
    raise RuntimeError("unknown local stt backend: " + backend)


class VoskStream:
    """発話中に逐次投入する streaming 認識器。

    vosk small-ja の RTF は RPi5 実測 0.93（= ほぼ実時間で追いつく）なので、
    発話が終わってから全体を流すと発話長ぶん丸ごと待たされる。フレームが届く
    そばから食わせておけば stop 時点の残りは 0.5 秒未満で済む。
    """

    CHUNK = 16000 * 2 // 2   # 0.5 秒ぶん（16k / 16bit）

    def __init__(self, rate: int = 16000):
        import vosk
        self._rec = vosk.KaldiRecognizer(_load_vosk(), float(rate))
        self._rec.SetWords(False)
        self._buf = bytearray()
        self._lock = asyncio.Lock()
        self.nbytes = 0

    async def feed(self, pcm: bytes):
        self.nbytes += len(pcm)
        self._buf.extend(pcm)
        if len(self._buf) < self.CHUNK:
            return
        chunk = bytes(self._buf)
        self._buf.clear()
        async with self._lock:
            await asyncio.to_thread(self._rec.AcceptWaveform, chunk)

    async def final(self) -> str:
        chunk = bytes(self._buf)
        self._buf.clear()
        async with self._lock:
            if chunk:
                await asyncio.to_thread(self._rec.AcceptWaveform, chunk)
            res = await asyncio.to_thread(self._rec.FinalResult)
        return _strip_spaces(json.loads(res).get("text", ""))


# ---- sherpa-onnx (ReazonSpeech k2 v2 zipformer transducer) --------------
SHERPA_DIR = os.environ.get("SHERPA_DIR", os.path.expanduser("~/models/reazonspeech-k2-v2"))
SHERPA_THREADS = int(os.environ.get("SHERPA_THREADS", "4"))
SHERPA_PAD = float(os.environ.get("SHERPA_PAD", "0.8"))   # 前後に足す無音 [s]
# 認識器に渡す前に声の大きさをそろえる（0 で無効）。実機の話しかけは
# rms 500〜1200 と小さく、そのまま渡すと雑音に弱い
SHERPA_NORM_RMS = float(os.environ.get("SHERPA_NORM_RMS", "3000"))
SHERPA_NORM_MAX_GAIN = float(os.environ.get("SHERPA_NORM_MAX_GAIN", "12"))
# 倍率の下限（1 なら小さくする方には働かない。0 で無効）
SHERPA_NORM_MIN_GAIN = float(os.environ.get("SHERPA_NORM_MIN_GAIN", "0"))
# 振り切れないよう、いちばん高い山をこの値に収める（0 で無効）
SHERPA_NORM_CLIP = float(os.environ.get("SHERPA_NORM_CLIP", "0.95"))
# これより小さい声は持ち上げない（雑音だけのバッファを増幅しないため）。
# app.py が「話しかけ」とみなす最大 rms のしきい値は 500
SHERPA_NORM_MIN_LEVEL = float(os.environ.get("SHERPA_NORM_MIN_LEVEL", "200"))
# 声の前後の黙っている所を切り落とす（0 で無効）。前後に残す秒数
SHERPA_TRIM = float(os.environ.get("SHERPA_TRIM", "0.4"))
_sherpa = None


def _load_sherpa():
    global _sherpa
    with _lock:
        if _sherpa is None:
            import sherpa_onnx
            d = SHERPA_DIR
            log.info("loading sherpa-onnx transducer from %s", d)
            _sherpa = sherpa_onnx.OfflineRecognizer.from_transducer(
                encoder=os.path.join(d, "encoder-epoch-99-avg-1.int8.onnx"),
                decoder=os.path.join(d, "decoder-epoch-99-avg-1.int8.onnx"),
                joiner=os.path.join(d, "joiner-epoch-99-avg-1.int8.onnx"),
                tokens=os.path.join(d, "tokens.txt"),
                num_threads=SHERPA_THREADS,
                sample_rate=16000,
                feature_dim=80,
                decoding_method="greedy_search",
            )
    return _sherpa


def _frame_thr(rms) -> float:
    """声とみなす音量の下限。切り出しにも音量そろえにも同じ物差しを使う。

    「いちばん大きいフレームの何割」という決め方だと、咳・ドア・机を叩く音が
    1 つ入っただけで本当の声がその下に沈む（査読の指摘。試験で再現した）。
    """
    import numpy as np
    # max ではなく上位 5% の位置＝60ms 一発の轟音では動かない
    loud = float(np.percentile(rms, 95))
    # 黙っている所の音量（＝雑音の高さ）。これを跨がないと切り出しが効かない
    floor = float(np.percentile(rms, 20))
    return max(loud * 0.2, floor * 2.0, 1e-4)


def _loud_frames(rms):
    """声とみなすフレームの真偽表。

    1 つも選ばれないとき（バッファ全部がほぼ同じ大きさで、黙っている所が
    無いとき）は、雑音を跨ぐための下限を外して選び直す。
    """
    import numpy as np
    sel = rms >= _frame_thr(rms)
    if not sel.any():
        sel = rms >= max(float(np.percentile(rms, 95)) * 0.2, 1e-4)
    return sel


def _frame_rms(audio, rate):
    """60ms ごとの音量を返す。フレームが 1 つも取れなければ None。"""
    import numpy as np
    step = max(1, int(rate * 0.06))
    n = len(audio) // step * step
    if n < step:
        return None, step
    frames = audio[:n].reshape(-1, step)
    return np.sqrt((frames * frames).mean(axis=1)), step


def _trim_to_speech(audio, rate, margin: float):
    """声が出ている所の前後だけ残す。

    黙っている時間ぶん認識器を回すのは遅いだけでなく、精度も落ちる（実測で
    前後 6 秒の雑音が付くと CER が倍近くになった）。声が見つからなければ何も
    しない（全部が雑音のバッファを短くしても意味がない）。
    """
    import numpy as np
    rms, step = _frame_rms(audio, rate)
    if rms is None or len(rms) < 2:
        return audio
    if float(rms.max()) <= 0.0:
        return audio
    loud = np.nonzero(_loud_frames(rms))[0]
    if len(loud) == 0:
        return audio
    m = int(rate * margin)
    a = max(0, int(loud[0]) * step - m)
    b = min(len(audio), (int(loud[-1]) + 1) * step + m)
    return audio[a:b]


def _speech_level(audio, rate) -> float:
    """声が出ている所だけの rms（16bit の目盛り）。

    実機が送ってくるバッファは 15 秒以上あって大半が無音のことがある。全体の
    rms で割ると、同じ声でも黙っていた長さで音量が変わってしまう。
    """
    import numpy as np
    rms, _step = _frame_rms(audio, rate)
    if rms is None:
        if len(audio) == 0:
            return 0.0
        return float(np.sqrt(np.mean(audio * audio)) * 32768.0)
    if float(rms.max()) <= 0.0:
        return 0.0
    loud = rms[_loud_frames(rms)]
    if len(loud) == 0:
        return 0.0
    # 平均ではなく中央値。轟音が 1 つ混じっても引きずられないため
    return float(np.median(loud) * 32768.0)


def _normalize(audio, rate):
    """声の大きさをそろえる（16bit の目盛りで SHERPA_NORM_RMS に合わせる）。"""
    import numpy as np
    level = _speech_level(audio, rate)
    if level <= SHERPA_NORM_MIN_LEVEL:
        return audio
    gain = min(SHERPA_NORM_RMS / level, SHERPA_NORM_MAX_GAIN)
    gain = max(gain, SHERPA_NORM_MIN_GAIN)
    if SHERPA_NORM_CLIP > 0.0:
        peak = float(np.abs(audio).max())
        if peak > 0.0:
            gain = min(gain, SHERPA_NORM_CLIP / peak)
    return np.clip(audio * gain, -1.0, 1.0).astype("float32")


def _sherpa_sync(pcm: bytes, rate: int) -> str:
    import numpy as np
    rec = _load_sherpa()
    audio = np.frombuffer(pcm, dtype="<i2").astype("float32") / 32768.0
    if SHERPA_TRIM > 0.0:
        audio = _trim_to_speech(audio, rate, SHERPA_TRIM)
    if SHERPA_NORM_RMS > 0.0:
        audio = _normalize(audio, rate)
    # 先頭・末尾に無音を足す（padding が無いと冒頭の一語を落とすことがある）
    pad = np.zeros(int(rate * SHERPA_PAD), dtype="float32")
    audio = np.concatenate([pad, audio, pad])
    s = rec.create_stream()
    s.accept_waveform(rate, audio)
    rec.decode_stream(s)
    return _strip_spaces(s.result.text)
