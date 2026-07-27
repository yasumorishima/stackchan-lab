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
SHERPA_PAD = float(os.environ.get("SHERPA_PAD", "0.3"))   # 前後に足す無音 [s]
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


def _sherpa_sync(pcm: bytes, rate: int) -> str:
    import numpy as np
    rec = _load_sherpa()
    audio = np.frombuffer(pcm, dtype="<i2").astype("float32") / 32768.0
    # 先頭・末尾に無音を足す（padding が無いと冒頭の一語を落とすことがある）
    pad = np.zeros(int(rate * SHERPA_PAD), dtype="float32")
    audio = np.concatenate([pad, audio, pad])
    s = rec.create_stream()
    s.accept_waveform(rate, audio)
    rec.decode_stream(s)
    return _strip_spaces(s.result.text)
