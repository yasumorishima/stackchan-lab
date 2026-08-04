"""応援歌を歌えるようにする（音源から旋律を起こし、公式の歌詞を乗せる）。

公式（`sp.baystars.co.jp`）にあるのは歌詞とふりがなだけで、メロディの数値
データはどこにも公開されていない。ドレミ表記を出している個人サイトは横浜の
分を持たず、載っている球団の分も画像だった。そこで**単旋律の歌唱音源から
自分で採譜する**（transcribe.py）。他人の採譜を写さずに済む。

音源は選手ごとの mp3 で、取ってきたものも起こした音符も
`~/stackchan-server/cache/songs/` に置くだけで、リポジトリには入れない
（歌詞と同じ扱い）。

  ./.venv/bin/python cheer_song.py 宮﨑
"""
import asyncio
import json
import logging
import os
import re
import sys

import server_tools
import transcribe

log = logging.getLogger("stackchan.song")

AUDIO_INDEX_URL = os.environ.get("CHEER_AUDIO_URL",
                                 "https://vocalo-oenka.com/baystars/")
CACHE_DIR = os.environ.get(
    "CHEER_CACHE", os.path.expanduser("~/stackchan-server/cache/songs"))
UA = {"User-Agent": "Mozilla/5.0"}
INDEX_TTL = 24 * 3600
_index = []          # [(取った時刻, {選手名: mp3 の URL})]

# 小さい仮名は前の字とひとまとまりで 1 音
_SMALL = "ゃゅょャュョぁぃぅぇぉァィゥェォ"
_KANA = re.compile("[ぁ-んァ-ヶーа-я]")


def moras(text):
    """歌詞を 1 音ずつに割る。「きょ」は 1 つ、「ー」は前に付ける。"""
    out = []
    for ch in text:
        if not _KANA.match(ch):
            continue
        if out and (ch in _SMALL or ch == "ー"):
            out[-1] += ch
        else:
            out.append(ch)
    return out


async def _fetch(session, url, binary=False):
    async with session.get(url, headers=UA, timeout=60) as r:
        r.raise_for_status()
        return (await r.read()) if binary else (await r.text())


async def audio_index(session):
    """選手名から応援歌の音源の場所を引く表を作る。"""
    import time
    now = time.monotonic()
    if _index and now - _index[0][0] < INDEX_TTL:
        return _index[0][1]
    html = await _fetch(session, AUDIO_INDEX_URL)
    table = {}
    for url in re.findall(r'https?://[^"\']+?\.mp3', html):
        name = url.rsplit("/", 1)[-1][:-4]
        name = re.sub(r"^[0-9]+[-.]?", "", name)      # 背番号を落とす
        name = name.replace("-", "").replace("_", "").replace("　", "")
        name = re.sub(r"^[A-Za-z]\.", "", name)       # 「G.後藤」の頭
        # 選手名でないファイル（キャッシュ避けの英数字だけの名前）は落とす
        if name and re.search(r"[ぁ-んァ-ヶ一-鿿]", name):
            table[name] = url
    _index[:] = [(now, table)]
    return table


def match_player(table, want):
    """言われた名前に近い選手を探す（姓だけでも当たるように）。"""
    want = (want or "").replace(" ", "").replace("　", "")
    if not want:
        return None
    if want in table:
        return want
    for name in table:
        if want in name or name in want:
            return name
    return None


def _cache_path(name, ext):
    os.makedirs(CACHE_DIR, exist_ok=True)
    safe = re.sub(r"[^\w぀-ヿ一-鿿]", "_", name)
    return os.path.join(CACHE_DIR, "%s.%s" % (safe, ext))


async def notes_for(session, name, url):
    """音源から音符を起こす（一度起こしたら取っておく）。"""
    js = _cache_path(name, "json")
    if os.path.exists(js):
        with open(js, encoding="utf-8") as f:
            d = json.load(f)
        return d["notes"], d["tempo"]
    mp3 = _cache_path(name, "mp3")
    if not os.path.exists(mp3):
        data = await _fetch(session, url, binary=True)
        with open(mp3, "wb") as f:
            f.write(data)
    notes, tempo, err = await asyncio.to_thread(transcribe.transcribe, mp3)
    log.info("%s の応援歌を採譜した（%d 音・テンポ %.0f・割り切れなさ %.2f）",
             name, sum(1 for n in notes if n[0]), tempo, err)
    with open(js, "w", encoding="utf-8") as f:
        json.dump({"notes": notes, "tempo": tempo}, f, ensure_ascii=False)
    return notes, tempo


def put_lyrics(notes, text):
    """音符に歌詞を 1 音ずつ乗せる。足りない分は伸ばす。"""
    ms = moras(text)
    out, i = [], 0
    for pitch, length in [(n[0], n[1]) for n in notes]:
        if pitch is None:
            out.append([None, length, ""])
            continue
        out.append([pitch, length, ms[i] if i < len(ms) else "ー"])
        i += 1
    return out, len(ms), sum(1 for n in notes if n[0])


async def prepare(session, want):
    """(音符, テンポ, 選手名) を返す。歌えなければ例外。"""
    table = await audio_index(session)
    name = match_player(table, want)
    if not name:
        raise LookupError("その選手の応援歌の音源が見つかりません")
    notes, tempo = await notes_for(session, name, table[name])
    songs = await server_tools._songs(session)
    key = server_tools._find_player(songs, name) or name
    text = "".join(songs.get(key, []))
    notes, n_mora, n_note = put_lyrics(notes, text)
    log.info("%s: 音符 %d に対して歌詞 %d 音", name, n_note, n_mora)
    return notes, tempo, name


if __name__ == "__main__":
    import aiohttp

    async def main():
        async with aiohttp.ClientSession() as s:
            notes, tempo, name = await prepare(s, sys.argv[1])
            print("%s / テンポ %.0f / %d 音" % (name, tempo, len(notes)))
            print([[n[0], n[1], n[2]] for n in notes][:20])

    asyncio.run(main())
