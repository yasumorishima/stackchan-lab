"""libopus の最小 ctypes バインディング。

スタックちゃん（XiaoZhi プロトコル）は生の Opus パケットを
WebSocket のバイナリフレームでやり取りする。Ogg コンテナは無い。
上り: 16000Hz mono / 60ms、下り: サーバーが hello で申告した値。
"""
import ctypes

_lib = ctypes.CDLL("libopus.so.0")

OPUS_APPLICATION_VOIP = 2048
_MAX_PACKET = 4000

_lib.opus_strerror.restype = ctypes.c_char_p
_lib.opus_strerror.argtypes = [ctypes.c_int]

_lib.opus_decoder_create.restype = ctypes.c_void_p
_lib.opus_decoder_create.argtypes = [ctypes.c_int32, ctypes.c_int,
                                     ctypes.POINTER(ctypes.c_int)]
_lib.opus_decoder_destroy.argtypes = [ctypes.c_void_p]
_lib.opus_decode.restype = ctypes.c_int
_lib.opus_decode.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int32,
                             ctypes.POINTER(ctypes.c_int16), ctypes.c_int,
                             ctypes.c_int]

_lib.opus_encoder_create.restype = ctypes.c_void_p
_lib.opus_encoder_create.argtypes = [ctypes.c_int32, ctypes.c_int, ctypes.c_int,
                                     ctypes.POINTER(ctypes.c_int)]
_lib.opus_encoder_destroy.argtypes = [ctypes.c_void_p]
_lib.opus_encode.restype = ctypes.c_int32
_lib.opus_encode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int16),
                             ctypes.c_int, ctypes.c_char_p, ctypes.c_int32]


def _check(code, what):
    if code < 0:
        raise RuntimeError("%s failed: %s" % (what, _lib.opus_strerror(code).decode()))


class Decoder:
    """Opus パケット -> 16bit PCM (little endian, mono)。"""

    def __init__(self, sample_rate=16000, channels=1):
        self.sample_rate = sample_rate
        self.channels = channels
        err = ctypes.c_int()
        self._st = _lib.opus_decoder_create(sample_rate, channels, ctypes.byref(err))
        _check(err.value, "opus_decoder_create")
        # 120ms 分あればどのフレーム長でも受けられる
        self._cap = int(sample_rate * 0.12)
        self._buf = (ctypes.c_int16 * (self._cap * channels))()

    def decode(self, packet: bytes) -> bytes:
        n = _lib.opus_decode(self._st, packet, len(packet), self._buf, self._cap, 0)
        _check(n, "opus_decode")
        return bytes(memoryview(self._buf).cast("B")[: n * self.channels * 2])

    def close(self):
        if self._st:
            _lib.opus_decoder_destroy(self._st)
            self._st = None


class Encoder:
    """16bit PCM (mono) -> Opus パケット。frame_ms 単位に切って返す。"""

    def __init__(self, sample_rate=24000, channels=1, frame_ms=60, bitrate=None):
        self.sample_rate = sample_rate
        self.channels = channels
        self.frame_ms = frame_ms
        self.frame_samples = sample_rate * frame_ms // 1000
        err = ctypes.c_int()
        self._st = _lib.opus_encoder_create(sample_rate, channels,
                                            OPUS_APPLICATION_VOIP, ctypes.byref(err))
        _check(err.value, "opus_encoder_create")
        self._out = ctypes.create_string_buffer(_MAX_PACKET)

    def encode_stream(self, pcm: bytes):
        """PCM 全体を frame_ms ごとの Opus パケット列にする。端数は無音で埋める。"""
        step = self.frame_samples * self.channels * 2
        for off in range(0, len(pcm), step):
            chunk = pcm[off:off + step]
            if len(chunk) < step:
                chunk = chunk + b"\x00" * (step - len(chunk))
            arr = (ctypes.c_int16 * (self.frame_samples * self.channels)).from_buffer_copy(chunk)
            n = _lib.opus_encode(self._st, arr, self.frame_samples, self._out, _MAX_PACKET)
            _check(n, "opus_encode")
            yield self._out.raw[:n]

    def close(self):
        if self._st:
            _lib.opus_encoder_destroy(self._st)
            self._st = None
