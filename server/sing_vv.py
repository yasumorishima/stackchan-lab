"""VOICEVOX の歌唱合成で歌わせる。

Sinsy（HMM）で作った歌は実機で「歌になっていない」と言われた。VOICEVOX 0.25 は
歌唱合成を持っていて、ずんだもん等の歌声が出せる。音符と歌詞の形はこちらの
ものをそのまま渡せる。

🔴 役が 2 つに分かれている（2026-08-05 実測）:
  - 楽譜を読む役 `/sing_frame_audio_query` は **type=sing のスタイルしか受けない**。
    0.25.2 に入っているのは **波音リツ ノーマル id=6000 の 1 つだけ**。
    ここにずんだもん 3003 を渡すと 500（`[singing_teacher, sing]` にスタイルが無い）。
  - 声を出す役 `/frame_synthesis` は type=frame_decode＝ずんだもん 3003 が使える。
  つまり **照会は 6000、合成は 3003** と分けて呼ぶ。

🔴 歌詞は 1 音 1 モーラしか受けない（2026-08-05 実測）:
  「ワー」「パン」「ー」は 400（`lyricが不正です`）。「ん」「っ」は 1 モーラとして
  通る。小書き仮名は **付けられる字が決まっている**（`きゃ`・`ふぁ` は可、
  `さぁ` は不可）＝下の `_COMBO` は 560 通りを実際に投げて通った 100 組。
  → こちらの歌詞は `cheer_song.moras()` が長音を前にくっつける（「ワー」）し、
    歌詞が音符より短いと単独の「ー」で埋める。**この境界で開いて渡す**。
    音符の中の長音は音の長さそのものなので落とし、音符をまたぐ長音は前の母音に開く。

  ./.venv/bin/python sing_vv.py <出力先.wav>
"""
import asyncio
import io
import json
import os
import sys
import re
import urllib.error
import urllib.parse
import urllib.request
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

VV = os.environ.get("VOICEVOX_URL", "http://127.0.0.1:50021")
SINGER = int(os.environ.get("VOICEVOX_SINGER", "3003"))   # 声（frame_decode）
SCORER = int(os.environ.get("VOICEVOX_SCORER", "6000"))   # 楽譜読み（sing）
TIMEOUT = float(os.environ.get("VOICEVOX_TIMEOUT", "300"))
FRAME_RATE = 93.75
CACHE_DIR = os.environ.get(
    "CHEER_CACHE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "cache", "songs"))


def _post(path, params, body):
    """VOICEVOX を叩く。断られたら**向こうの言い分をそのまま**例外に載せる。

    載せないと 400 の理由（どの歌詞が不正か等）が分からず、毎回調べ直しになる。
    """
    url = VV + path + "?" + urllib.parse.urlencode(params)
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        raise RuntimeError("VOICEVOX %s が %d: %s"
                           % (path, e.code, detail)) from None


_STEP = {"C": 0, "C#": 1, "D": 2, "D#": 3, "E": 4, "F": 5, "F#": 6,
         "G": 7, "G#": 8, "A": 9, "A#": 10, "B": 11}


def to_key(name):
    """"A#4" → MIDI の番号。"""
    return 12 * (int(name[-1]) + 1) + _STEP[name[:-1]]


# --- 歌詞を VOICEVOX が受ける形にする -------------------------------------
# 2026-08-05 に 560 通り投げて通った 100 組（VOICEVOX 0.25.2）。
_COMBO = set(
    "いぇ うぃ うぅ うぇ うぉ きゃ きゅ きょ きぃ きぇ くぁ くぃ くぅ くぇ くぉ "
    "しゃ しゅ しょ しぇ すぃ ちゃ ちゅ ちょ ちぇ つぁ つぃ つぇ つぉ "
    "てゃ てゅ てょ てぃ てぇ とぅ にゃ にゅ にょ にぃ にぇ ひゃ ひゅ ひょ ひぃ ひぇ "
    "ふぁ ふぃ ふぇ ふぉ みゃ みゅ みょ みぃ みぇ りゃ りゅ りょ りぃ りぇ "
    "ぎゃ ぎゅ ぎょ ぎぃ ぎぇ ぐぁ ぐぃ ぐぅ ぐぇ ぐぉ じゃ じゅ じょ じぇ ずぃ "
    "ぢゃ ぢゅ ぢょ ぢぇ でゃ でゅ でょ でぃ でぇ どぅ びゃ びゅ びょ びぃ びぇ "
    "ぴゃ ぴゅ ぴょ ぴぃ ぴぇ ゔゃ ゔゅ ゔょ ゔぁ ゔぃ ゔぇ ゔぉ".split())
_SMALL_VOWEL = {"ぁ": "あ", "ぃ": "い", "ぅ": "う", "ぇ": "え", "ぉ": "お"}
_SMALL_Y = {"ゃ": "や", "ゅ": "ゆ", "ょ": "よ", "ゎ": "わ"}
_SMALL = set(_SMALL_VOWEL) | set(_SMALL_Y)
_LONG = "ー〜～-‐−"
_VOWEL_ROW = {
    "あ": "あかさたなはまやらわがざだばぱ",
    "い": "いきしちにひみりゐぎじぢびぴ",
    "う": "うくすつぬふむゆるぐずづぶぷゔ",
    "え": "えけせてねへめれゑげぜでべぺ",
    "お": "おこそとのほもよろをごぞどぼぽ",
}
_VOWEL_OF = {ch: v for v, chars in _VOWEL_ROW.items() for ch in chars}
_VOWEL_OF.update({s: v for s, v in _SMALL_VOWEL.items()})
_VOWEL_OF.update({"ゃ": "あ", "ゅ": "う", "ょ": "お", "ゎ": "あ", "ん": "ん"})
_KANA = set(_VOWEL_OF) | {"っ"}


def _hira(text):
    """カタカナをひらがなに。「ヵヶ」も寄せる。長音記号はそのまま。"""
    out = []
    for ch in text:
        if ch == "ヵ":
            out.append("か")
        elif ch == "ヶ":
            out.append("け")
        elif "ァ" <= ch <= "ヶ":
            out.append(chr(ord(ch) - 0x60))
        else:
            out.append(ch)
    return "".join(out)


def _vowel_of(mora):
    """そのモーラを伸ばすと出る音。「きょ」→「お」、「ん」→「ん」。"""
    for ch in reversed(mora):
        if ch in _VOWEL_OF:
            return _VOWEL_OF[ch]
    return None


def to_morae(lyric, prev=None):
    """歌詞 1 つを、VOICEVOX が受ける 1 モーラの並びにする。

    返すのは (モーラの並び, 次に渡す母音)。読めない字は落とす。

    - 音符の中の長音（「ワー」）は音の長さで表すので落とす
    - 音符をまたぐ長音（単独の「ー」）は前の母音に開く
    - 小書き仮名は付けられる字（`_COMBO`）のときだけ前にくっつける。
      付かない小書き母音（「さぁ」の「ぁ」）は長音と同じ扱い＝前と同じ母音なら
      落とし、違えば大きい字にして 1 モーラ立てる
    """
    out = []
    for ch in _hira(str(lyric or "")):
        if ch in _LONG:
            if not out and prev:              # 音符をまたぐ長音
                out.append(prev)
                prev = _vowel_of(prev)
            continue                          # 音符の中の長音は落とす
        if ch in _SMALL:
            if out and (out[-1][-1] + ch) in _COMBO:
                out[-1] += ch                 # 「きゃ」で 1 モーラ
                continue
            big = _SMALL_VOWEL.get(ch) or _SMALL_Y[ch]
            if out and _vowel_of(out[-1]) == big:
                continue                      # 「さぁ」＝長音と同じ
            out.append(big)
            continue
        if ch in _KANA:
            out.append(ch)
    if out:
        prev = _vowel_of(out[-1]) or prev
    return out, prev


# 声の出る帯（2026-08-05 実測・ずんだもん 3003）。1 音ずつ鳴らして測ったところ
# **E3(52) より下は書いた高さで鳴らない**（40〜49 を渡すと 65〜73 が鳴る）。
# 応援歌の音源は男声なので、そのまま渡すと丸ごとこの帯の下に落ちる（実測: 牧の
# 楽譜は 40〜55 で、鳴った音との差は平均 11.21 半音だった）。**旋律の形は変えず、
# オクターブ単位で真ん中へ寄せる**。
FIT_LO = int(os.environ.get("VOICEVOX_FIT_LO", "64"))   # E4
FIT_HI = int(os.environ.get("VOICEVOX_FIT_HI", "72"))   # C5


def octave_shift(keys):
    """中央が帯に入るまでの移調量（12 の倍数）。"""
    if not keys:
        return 0
    mid = sorted(keys)[len(keys) // 2]
    shift = 0
    while mid + shift < FIT_LO:
        shift += 12
    while mid + shift > FIT_HI:
        shift -= 12
    return shift


def _spread(frames, n):
    """フレーム数を n 個に割る。端数は先頭に寄せる。短すぎるなら None。"""
    if n <= 1:
        return [frames]
    each = max(2, frames // n)
    out = [each] * n
    out[0] += frames - each * n
    return out if out[0] >= 2 else None


# 曲中の休みはここまで（秒）。音源の頭には前置き（イントロ）があり、そのまま
# 起こすと歌い出しが 1 秒以上遅れる。曲中にも 1 秒級の切れ目が残ることがあり、
# ひとりで歌うぶんには**間が持たず止まって聞こえる**（実測 2026-08-05: 森敬斗
# 1.07 秒・筒香嘉智 1.09 秒・その他の左打者 1.18 秒）。頭と終わりの休みは
# こちらで置く前後の休みがあるので落とす。
MAX_REST_SEC = float(os.environ.get("SING_MAX_REST", "0.8"))


def trim_rests(notes):
    """頭と終わりの休みを落とし、間の休みを頭打ちにする。"""
    out = list(notes)
    while out and out[0][0] is None:
        out.pop(0)
    while out and out[-1][0] is None:
        out.pop()
    return out


def to_score(notes, tempo, frame_rate=FRAME_RATE):
    """こちらの音符を VOICEVOX の楽譜にする。

    長さは 16分音符いくつ分。フレーム数に直す。歌詞は 1 音 1 モーラ。
    2 モーラ以上あれば同じ高さのまま音符を割る。前後に休みを置く
    （歌い出しと歌い終わりに要る）。
    """
    unit = 60.0 / tempo / 4.0
    notes = trim_rests(notes)
    max_rest = max(1, int(round(MAX_REST_SEC / unit)))
    out = [{"key": None, "frame_length": int(frame_rate * 0.5), "lyric": ""}]
    prev = None
    shift = octave_shift([to_key(n[0]) for n in notes if n[0]])
    for pitch, length, lyric in [(n[0], n[1], n[2]) for n in notes]:
        if pitch is None:
            length = min(length, max_rest)
        frames = max(2, int(round(length * unit * frame_rate)))
        if pitch is None:
            out.append({"key": None, "frame_length": frames, "lyric": ""})
            continue
        morae, prev = to_morae(lyric, prev)
        if not morae:
            morae = [prev or "ら"]
            prev = _vowel_of(morae[0]) or prev
        share = _spread(frames, len(morae))
        if share is None:                     # 割ると短すぎるなら先頭だけ
            morae, share = morae[:1], [frames]
        key = to_key(pitch) + shift
        for mora, f in zip(morae, share):
            out.append({"key": key, "frame_length": f, "lyric": mora})
    out.append({"key": None, "frame_length": int(frame_rate * 0.4),
                "lyric": ""})
    return {"notes": out}


# 出来た歌は取っておく。VOICEVOX は 2.68GB 常駐する上に合成に数十秒かかるので、
# 頼まれてから作っていると RPi5（共有機）を占有し、返事も待たせる。曲は数えるほど
# しかないので、先に作って置いておけば **VOICEVOX は普段止めておける**。
# 名前に版を入れて、起こし方や渡し方を変えたら作り直しになるようにする。
SUNG_VERSION = 2


def sung_path(name):
    import transcribe
    safe = re.sub(r"[^\w぀-ヿ一-鿿]", "_", name)
    return os.path.join(CACHE_DIR, "%s.sung.t%ds%d.wav"
                        % (safe, transcribe.VERSION, SUNG_VERSION))


def sung(name, notes, tempo):
    """取ってあればそれを、無ければ作って取っておく。(pcm, 周波数) を返す。"""
    path = sung_path(name)
    if os.path.exists(path):
        with wave.open(path, "rb") as w:
            return w.readframes(w.getnframes()), w.getframerate()
    pcm, rate = sing_sync(notes, tempo)
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with wave.open(tmp, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    os.replace(tmp, path)
    return pcm, rate


def sing_sync(notes, tempo, singer=SINGER, scorer=SCORER):
    """歌わせて (pcm, サンプリング周波数) を返す。"""
    query = json.loads(_post("/sing_frame_audio_query", {"speaker": scorer},
                             to_score(notes, tempo)))
    wav = _post("/frame_synthesis", {"speaker": singer}, query)
    with wave.open(io.BytesIO(wav), "rb") as w:
        return w.readframes(w.getnframes()), w.getframerate()


async def sing(name, notes, tempo):
    """歌を返す。取ってあれば読むだけ（VOICEVOX を起こさない）。"""
    return await asyncio.to_thread(sung, name, notes, tempo)


if __name__ == "__main__":
    import aiohttp

    import cheer_song

    async def main():
        async with aiohttp.ClientSession() as s:
            notes, tempo, name = await cheer_song.prepare(
                s, os.environ.get("WHO", "宮﨑"))
        pcm, rate = sing_sync(notes, tempo)
        with wave.open(sys.argv[1], "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(pcm)
        print("%s / 音符 %d / %.1f 秒 / %d Hz → %s"
              % (name, len(notes), len(pcm) / 2 / rate, rate, sys.argv[1]))

    asyncio.run(main())
