"""応援歌を歌えるようにする（音源から旋律を起こし、公式の歌詞を乗せる）。

公式（`sp.baystars.co.jp`）にあるのは歌詞とふりがなだけで、メロディの数値
データはどこにも公開されていない。ドレミ表記を出している個人サイトは横浜の
分を持たず、載っている球団の分も画像だった。そこで**単旋律の歌唱音源から
自分で採譜する**（transcribe.py）。他人の採譜を写さずに済む。

**音源は今の選手が載っている表から取る。** 最初に使った 2017 年の一覧は、
いまの歌詞 25 件のうち 3 件しか重ならなかった（user 指摘「いま 2026 年だよ」）。
選手ごとの表がある方のページには 69 曲あり、牧・佐野・神里なども載っている。

取ってきた音源も起こした音符も `~/stackchan-server/cache/songs/` に置くだけで、
リポジトリには入れない（歌詞と同じ扱い）。

  ./.venv/bin/python cheer_song.py 牧
"""
import asyncio
import json
import logging
import os
import re
import time

import segment_dp
import server_tools
import sheet_song
import transcribe

log = logging.getLogger("stackchan.song")

AUDIO_INDEX_URL = os.environ.get(
    "CHEER_AUDIO_URL", "https://vocalo-oenka.com/purosupi-baystars/")
CACHE_DIR = os.environ.get(
    "CHEER_CACHE", os.path.expanduser("~/stackchan-server/cache/songs"))
SHEET_DIR = os.environ.get(
    "CHEER_SHEET_DIR", os.path.expanduser("~/stackchan-server/cache/sheets"))
UA = {"User-Agent": "Mozilla/5.0"}
INDEX_TTL = 24 * 3600
# 起こした音符のうち休みがこれを超える曲は歌わない。歌ではなく短い音の連打に
# なるため（実測: 宮﨑敏郎 0% / 牧秀悟 33%、後者は実機で「歌えてない」）
MAX_GAP = float(os.environ.get("CHEER_MAX_GAP", "0.20"))
_index = []          # [(取った時刻, {選手名: 音源の URL})]

# 音源側の呼び方と、公式の歌詞の見出しの対応
_ALIAS = {"右打者汎用": "その他の右打者", "左打者汎用": "その他の左打者",
          "捕手汎用": "捕手のテーマ", "投手汎用": "投手のテーマ（右投手）"}

# 応援歌は前の選手の曲を歌詞替えで使うことがある（音源側の表も「牧秀悟
# （村田修一流用）」と書いている）。旋律が同じなら、流用元の音源に今の歌詞を
# 乗せれば歌える。`www.yakyu-ouen.net` の各選手ページに流用の記載がある
_REUSE = {"梶原昂希": "下園辰哉"}

# 小さい仮名は前の字とひとまとまりで 1 音
_SMALL = "ゃゅょャュョぁぃぅぇぉァィゥェォ"
_KANA = re.compile("[ぁ-んァ-ヶー]")
_AUDIO = re.compile(r'https?://[^"\']+?\.(?:m4a|mp3)')
_CELL = re.compile(r'<t[dh][^>]*>\s*([^<>]{2,24}?)\s*</t[dh]>')


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


def clean_name(s):
    """「牧秀悟（村田修一流用）」→「牧秀悟」。汎用は公式の見出しに寄せる。"""
    s = re.sub(r"（[^）]*）", "", s).strip()
    return _ALIAS.get(s, s)


def parse_index(html):
    """表から (選手名 → 音源) を作る。名前の枠は音源より前に出てくる。"""
    cells = [(m.start(), m.group(1)) for m in _CELL.finditer(html)]
    table = {}
    for m in _AUDIO.finditer(html):
        before = [c for p, c in cells if p < m.start() and c != "パス"]
        if not before:
            continue
        name = clean_name(before[-1])
        if name and not re.fullmatch(r"[A-Za-z0-9 ._-]+", name):
            table.setdefault(name, m.group(0))
    return table


async def _fetch(session, url, binary=False):
    async with session.get(url, headers=UA, timeout=60) as r:
        r.raise_for_status()
        return (await r.read()) if binary else (await r.text())


async def audio_index(session):
    """選手名から応援歌の音源の場所を引く表を作る。"""
    now = time.monotonic()
    if _index and now - _index[0][0] < INDEX_TTL:
        return _index[0][1]
    table = parse_index(await _fetch(session, AUDIO_INDEX_URL))
    _index[:] = [(now, table)]
    return table


def match_player(table, want):
    """言われた名前に近い選手を探す（姓だけでも当たるように）。"""
    want = clean_name((want or "").replace(" ", "").replace("　", "")
                      .replace("選手", ""))
    if not want:
        return None
    if want in table:
        return want
    hit = [n for n in table if want in n or n in want]
    # 1 人に絞れないなら**当てない**（「森」で 2 人当たるのに先頭を返すと、
    # 別人の旋律で歌ってしまう）
    return hit[0] if len(hit) == 1 else None


def _cache_path(name, ext):
    os.makedirs(CACHE_DIR, exist_ok=True)
    safe = re.sub(r"[^\w぀-ヿ一-鿿]", "_", name)
    return os.path.join(CACHE_DIR, "%s.%s" % (safe, ext))


def local_audio(name):
    """`cache/songs/<選手名>.<拡張子>` に置いてある音源を探す。

    配布ページに音源が無い選手でも、ここに音を置けば歌える。取得元は問わない
    （公開されている音源が無いものは、ここに置いてもらう以外に手が無い）。
    """
    for ext in ("mp3", "m4a", "wav", "ogg", "flac"):
        path = _cache_path(name, ext)
        if os.path.exists(path):
            return path
    return None


def sheet_source(name):
    """譜面から起こした旋律（JSON、sheet_song 参照）。音源が無い選手用。"""
    path = os.path.join(SHEET_DIR,
                        "%s.json" % re.sub(r"[^\w぀-ヿ一-鿿]", "_", name))
    return path if os.path.exists(path) else None


# 採譜のやり方を変えたら上げる（取ってある音符を捨てるため）。
# 1 = 高さの変わり目で切る（〜2026-08-05・リズムが合わず外した）
# 2 = DP でモーラ数に区切る（2026-08-06〜）
SEG_VERSION = 2
# 1 音がこれより長い曲は、音源が歌詞を覆っていない＝歌わない（秒）
MAX_SEC_PER_MORA = float(os.environ.get("SING_MAX_SEC_PER_MORA", "0.8"))


async def notes_by_morae(name, path, morae):
    """歌詞のモーラ数に合わせて音源を区切る（DP）。

    ⚠️ 高さの変わり目で切る旧方式は、同じ高さで続く歌詞が 1 音に潰れて
    以降が全部ずれる。**音符の数を歌詞のモーラ数に固定する**のが要点。
    """
    js = _cache_path(name, "seg%d.json" % SEG_VERSION)
    if os.path.exists(js) and os.path.getmtime(js) >= os.path.getmtime(path):
        with open(js, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("v") == SEG_VERSION and d.get("morae") == len(morae):
            return d["notes"], d["tempo"]
    notes, tempo, _edges = await asyncio.to_thread(
        segment_dp.build, path, morae)
    log.info("%s の応援歌を %s から採譜した（%d 音・歌詞 %d モーラ）",
             name, os.path.basename(path), len(notes), len(morae))
    with open(js, "w", encoding="utf-8") as f:
        json.dump({"v": SEG_VERSION, "morae": len(morae),
                   "notes": notes, "tempo": tempo}, f, ensure_ascii=False)
    return notes, tempo


async def notes_from_file(name, path):
    """手元の音源から音符を起こす（一度起こしたら取っておく）。"""
    js = _cache_path(name, "json")
    if os.path.exists(js) and os.path.getmtime(js) >= os.path.getmtime(path):
        with open(js, encoding="utf-8") as f:
            d = json.load(f)
        # 起こし方を変えたら取ってある結果は使わない（音源の更新時刻だけでは
        # 気付けない）
        if d.get("v") == transcribe.VERSION:
            return d["notes"], d["tempo"]
    notes, tempo, err = await asyncio.to_thread(transcribe.transcribe, path)
    log.info("%s の応援歌を %s から採譜した"
             "（%d 音・テンポ %.0f・割り切れなさ %.2f）",
             name, os.path.basename(path),
             sum(1 for n in notes if n[0]), tempo, err)
    with open(js, "w", encoding="utf-8") as f:
        json.dump({"v": transcribe.VERSION, "notes": notes,
                   "tempo": tempo}, f, ensure_ascii=False)
    return notes, tempo


async def notes_for(session, name, url):
    """配布ページの音源から音符を起こす（音は取ってきて置いておく）。

    音源のパスも返す（DP はあとで音源を読み直すため）。
    """
    path = local_audio(name)
    if path is None:
        path = _cache_path(name, url.rsplit(".", 1)[-1])
        with open(path, "wb") as f:
            f.write(await _fetch(session, url, binary=True))
    notes, tempo = await notes_from_file(name, path)
    return notes, tempo, path


def gap_ratio(notes):
    """音符の並びのうち、休みが占める割合。"""
    total = sum(n[1] for n in notes) or 1
    return sum(n[1] for n in notes if not n[0]) / float(total)


async def _prepare_sheet(key, songs):
    """譜面 JSON から歌う。音源と違い休符も長さも譜面どおりなので、
    休み割合や 秒/モーラ の門番（音源と歌詞の対応の検査）は通さない。"""
    path = sheet_source(key)
    if path is None:
        raise LookupError("音源が無い")
    # 譜面 JSON の破損・欠落・歌詞との不整合は**恒久的なデータ不良**。
    # 汎用の except に落とすと「いま歌えませんでした」（一時エラー風の
    # 返事）になり何度でも呼ばれるので、LookupError（「その選手の応援歌
    # は歌えません」側）に写像する（監査指摘 3）
    try:
        with open(path, encoding="utf-8") as f:
            sheet = json.load(f)
        notes, tempo, nm, _call = sheet_song.build(sheet, songs[key], moras, key)
    except LookupError:
        raise
    except Exception as e:
        log.warning("%s の譜面が読めない: %s: %s", key, type(e).__name__, e)
        raise LookupError("譜面が読めない")
    log.info("%s: 譜面から歌う（音符 %d・歌詞 %d モーラ・テンポ %.0f）",
             key, len(notes), nm, tempo)
    return notes, tempo, key


async def prepare(session, want):
    """(音符, テンポ, 見出し) を返す。歌えなければ LookupError。"""
    songs = await server_tools._songs(session)
    key = await asyncio.to_thread(server_tools._find_player,
                                  songs, want)
    # 公式に歌詞が無い選手（もう在籍していない等）は、音だけ鳴らしても
    # 意味が無いので歌わない
    text = "".join(songs.get(key, [])) if key else ""
    if not text:
        raise LookupError("歌詞が無い")
    # 最後の「かっとばせー！○○！」はコール＝歌ではないので歌わない。
    # 譜面から歌う経路（sheet_song.build）と同じ判定を使う
    sung_lines, _call = sheet_song.split_call(list(songs.get(key, [])))
    # 先頭の掛け声（オオオオー…）も歌わない。音程を持たないので旋律に
    # 乗せると歌にならない（実機 2026-08-08「牧の応援歌、めちゃくちゃ」）
    sung_lines = sheet_song.drop_chant(sung_lines, key)
    text = "".join(sung_lines) or text

    # 譜面（ゲームの応援歌エディタ）から起こした旋律があるならそれを使う。
    # 音源からの採譜は自前の音高追跡が入るぶん誤差が残る（実測 宮﨑: 楽譜に
    # オクターブの飛び値／楽譜 43 音に対し鳴った音 62）のに対し、譜面は高さと
    # 長さが離散値で書いてある。**ここに置いてあること自体が「読めた」の印**で、
    # 繰り返しの見立てが怪しい曲は置かない（判定を置き場で表す）
    if sheet_source(key):
        return await _prepare_sheet(key, songs)

    # 置いてもらった音源（配布ページに無い選手はこれしか手が無い）
    path = local_audio(key)
    if path:
        notes, tempo = await notes_from_file(key, path)
    else:
        table = await audio_index(session)
        name = match_player(table, want) or match_player(table,
                                                         _REUSE.get(key, ""))
        if not name:
            # 配布ページに音源が無い選手は、譜面から起こした旋律で歌う
            return await _prepare_sheet(key, songs)
        notes, tempo, path = await notes_for(session, name, table[name])
    # 休みだらけの音源（歌が入っていない）はここで外す。判定は旧採譜の
    # 音符の並びで見る＝DP は必ずモーラ数だけ音符を作るので休みが出ない
    gap = gap_ratio(notes)
    if gap > MAX_GAP:
        log.info("%s は休みが %.0f%% で歌にならないので歌わない",
                 key, gap * 100)
        raise LookupError("歌にならない")
    morae = moras(text)
    if not morae:
        raise LookupError("歌詞が読めない")
    notes, tempo = await notes_by_morae(key, path, morae)
    # 🔴 音源が歌詞を 1 対 1 で覆っていない曲は歌わない。DP は必ずモーラ数だけ
    # 音符を作るので、覆っていない曲では 1 音が異様に伸びて歌にならない。
    # 実測（2026-08-06・13 曲）: 12 曲は 0.22〜0.58 秒/モーラに固まり、
    # 「その他の右打者」だけ 1.59 秒（歌詞 14 モーラに対し 22.2 秒鳴っている）。
    # その曲は楽譜との差 3.17 半音・最長無音 1.46 秒で、聴ける歌になっていない
    spm = sum(n[1] for n in notes) * 0.01 / max(len(morae), 1)
    if spm > MAX_SEC_PER_MORA:
        log.info("%s は 1 音 %.2f 秒＝歌詞と音源が対応していないので歌わない",
                 key, spm)
        raise LookupError("歌詞と音源が合っていない")
    log.info("%s: 歌詞 %d モーラに対して音符 %d（1 音 %.2f 秒）",
             key, len(morae), len(notes), spm)
    return notes, tempo, key


def _cached_ok(name):
    """一度起こしてあって、歌になる曲か。まだ起こしていなければ候補に残す。"""
    js = _cache_path(name, "json")
    if not os.path.exists(js):
        return True
    try:
        with open(js, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("v") != transcribe.VERSION:
            return True          # 起こし直せば歌えるかもしれない
        return gap_ratio(d["notes"]) <= MAX_GAP
    except Exception:
        return False


async def singable(session):
    """いま歌える見出しの一覧（音源と歌詞の両方があるもの）。"""
    songs = await server_tools._songs(session)
    table = await audio_index(session)
    out = []
    for name in table:
        key = await asyncio.to_thread(server_tools._find_player,
                                      songs, name)
        if key and songs.get(key) and _cached_ok(name):
            out.append(key)
    for key, src in _REUSE.items():
        if songs.get(key) and match_player(table, src):
            out.append(key)
    for key in songs:                       # 手元に置いてもらった音源
        if songs.get(key) and local_audio(key):
            out.append(key)
    for key in songs:                       # 譜面から起こした旋律
        if songs.get(key) and sheet_source(key):
            out.append(key)
    return sorted(set(out))


if __name__ == "__main__":
    import sys

    import aiohttp

    async def main():
        async with aiohttp.ClientSession() as s:
            if len(sys.argv) < 2:
                names = await singable(s)
                print("歌えるのは %d 件: %s" % (len(names), "、".join(names)))
                return
            notes, tempo, name = await prepare(s, sys.argv[1])
            print("%s / テンポ %.0f / %d 音" % (name, tempo, len(notes)))
            print([[n[0], n[1], n[2]] for n in notes][:16])

    asyncio.run(main())
