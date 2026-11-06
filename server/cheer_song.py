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
import team_song
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
# 1 にすると、譜面の無い曲も原曲の音源から採って歌う（旋律が合わないので普段は 0）
FROM_AUDIO = os.environ.get("SING_FROM_AUDIO", "0") == "1"
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


per_note = sheet_song.per_note


async def _prepare_sheet(key, songs):
    """譜面 JSON から歌う。音源と違い休符も長さも譜面どおりなので、
    休み割合や 秒/モーラ の門番（音源と歌詞の対応の検査）は通さない。"""
    path = sheet_source(key)
    if path is None:
        raise LookupError("音源が無い")
    spec = _style_table().get(key, {}).get("per_note")
    if spec:
        # 🔴 牧: 音源の歌の部分（15.2 秒〜）には旋律の楽器が無く、音から
        # 採るとベースを拾っていた（user 実機 2026-11-06「メロディが違う」）。
        # 譜面の 8 小節＝歌の 8 小節に、教わった歌い方を 1 音ずつ乗せる
        try:
            with open(path, encoding="utf-8") as f:
                notes, tempo = per_note(json.load(f), spec)
        except ValueError as e:
            log.warning("%s: %s", key, e)
            raise LookupError("譜面に歌詞が乗らない")
        log.info("%s: 譜面に 1 音ずつ歌詞を乗せて歌う（%d 音）", key, len(notes))
        return notes, tempo, key
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


# 🔴 前奏と歌で歌詞が違う曲（前奏は掛け声の歌詞）は、全歌詞を曲全体へ均等に
# 合わせると歌の頭が前奏に食い込む（user 実機 2026-11-05「牧と筒香は前奏が
# 違う歌詞なのに、歌おうとして間違ってる」「前奏も正しい歌詞で歌えばいい」）。
# 音源で歌が始まる秒を曲ごとに持ち、**そこから後ろだけを歌の行に合わせて歌う**
# （前奏も歌わせたら駄目だった。user 実機 2026-11-05「牧と筒香は前奏なしで」）。
# 牧: 2.8〜9 秒の旋律が 16.7 秒からもう一度出る（音高の並びが一致）＝後半が歌。
# 筒香: 13.8 秒からの長い音のあと 15.7 秒から歌（音量も 16 秒台で一段上がる）
# 値は (歌が始まる秒, 最後のコールも音源にあるか)。牧の後半は 8.5 秒で
# 歌詞 25 モーラ＝前奏と同じ 1 音 0.34 秒になり、コールの入る余地が無い
SUNG_FROM_SEC = {"牧秀悟": (16.6, False), "筒香嘉智": (15.6, True)}


def _cut(path, start, end=None):
    """音源の start〜end 秒を wav に切り出して置く（一度作れば使い回す）。"""
    out = "%s.cut%g-%s.wav" % (os.path.splitext(path)[0], start,
                               "%g" % end if end is not None else "end")
    if not os.path.exists(out) or os.path.getmtime(out) < os.path.getmtime(path):
        import subprocess
        cmd = ["ffmpeg", "-v", "error", "-y", "-ss", str(start), "-i", path]
        if end is not None:
            cmd += ["-t", str(end - start)]
        subprocess.run(cmd + ["-ac", "1", out], check=True)
    return out


def _style_table():
    """伸ばし方の置き場（非公開・cache/sheets/sung_style.json）。

    形: {選手: {公式の行: 実際の歌い方}}。歌い方は「めざーせ」「ホームラーン」の
    ように**伸ばす音に「ー」を付けた行**（user 様に教わった形のまま）。
    """
    path = os.path.join(SHEET_DIR, "sung_style.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.warning("sung_style.json が読めない: %s", e)
        return {}


def restyle(notes, lines, style):
    """行ごとの長さは音源のまま、行の中の音の長さを歌い方の「ー」で配り直す。

    音源から起こした音符は、どの音もほぼ同じ長さ（0.3 秒前後）に並んでいた。
    「勝利をさあめざーせ」「ホームラーン」の伸ばしが無く、user 実機で
    「歌ひどい」（2026-11-05）。ー の付いた音を 2、他を 1 の重みで行の長さを
    分ける。音の高さは、配り直した音と一番長く重なる元の音のもの。
    notes は [高さ, 長さ, 歌詞]（休みは高さ None）、lines は公式の行。
    """
    sung = []                      # (始まり, 長さ, 高さ) 歌う音だけ
    t = 0
    rests = {}                     # 歌う音の番号 → 直前の休み
    for n in notes:
        if n[0] is None:
            rests[len(sung)] = rests.get(len(sung), 0) + n[1]
        else:
            sung.append((t, n[1], n[0]))
        t += n[1]
    out, i = [], 0
    for ln in lines:
        k = len(moras(ln))
        part = sung[i:i + k]
        want = style.get(ln)
        if want and len(part) == k and k:
            ms = moras(want)
            w = [1 + m.count("ー") for m in ms]
            t0, t1 = part[0][0], part[-1][0] + part[-1][1]
            span, acc = t1 - t0, 0
            if rests.get(i):
                out.append([None, rests[i], ""])
            for m, wi in zip(ms, w):
                a = t0 + span * acc // sum(w)
                acc += wi
                b = t0 + span * acc // sum(w)
                best = max(part, key=lambda q: min(b, q[0] + q[1]) - max(a, q[0]))
                out.append([best[2], b - a, m])
        else:
            for j, q in enumerate(part):
                if rests.get(i + j):
                    out.append([None, rests[i + j], ""])
                out.append([q[2], q[1], notes_lyric(notes, i + j)])
        i += k
    for j, q in enumerate(sung[i:]):
        if rests.get(i + j):
            out.append([None, rests[i + j], ""])
        out.append([q[2], q[1], notes_lyric(notes, i + j)])
    return out


def notes_lyric(notes, idx):
    """歌う音の idx 番目の歌詞。"""
    return [n for n in notes if n[0] is not None][idx][2]


async def _prepare_two_parts(key, path, songs, sung_lines):
    """前奏は歌わず、音源の from_sec 秒から後ろだけを歌の行に合わせて歌う。"""
    from_sec, with_tail = SUNG_FROM_SEC[key]
    # sung_lines は助詞の読みを直した後の行なので、こちらも直してから比べる
    lines = [sheet_song.read_particles(x) for x in
             sheet_song.drop_notes(list(songs.get(key, [])))]
    first = next((i for i, ln in enumerate(lines) if ln in sung_lines), 0)
    # 🔴 前奏は歌わない（user 実機 2026-11-05「牧と筒香は前奏なしで」）。
    # 前奏を掛け声の歌詞で歌わせたら駄目だった。歌は from_sec から
    body = lines[first:]
    if not with_tail:
        while body and body[-1] not in sung_lines:
            body.pop()
    tail = 0
    for ln in reversed(body):
        if ln in sung_lines:
            break
        tail += len(moras(ln))
    n2, t2 = await notes_by_morae(
        key + "_from%g" % from_sec,
        await asyncio.to_thread(_cut, path, from_sec),
        moras("".join(body)))
    notes = n2[:len(n2) - tail] if tail else n2
    n_morae = len(n2) - tail
    style = _style_table().get(key)
    if style:
        notes = restyle(notes, body, style)
    spm = sum(n[1] for n in notes) * 0.01 / max(n_morae, 1)
    if spm > MAX_SEC_PER_MORA:
        log.info("%s は 1 音 %.2f 秒＝歌詞と音源が対応していないので歌わない",
                 key, spm)
        raise LookupError("歌詞と音源が合っていない")
    log.info("%s: 前奏を除き %.1f 秒からの歌 %d 行を歌う（%d 音・1 音 %.2f 秒）",
             key, from_sec, len(body), len(notes), spm)
    return notes, t2, key


async def prepare(session, want):
    """(音符, テンポ, 見出し) を返す。歌えなければ LookupError。"""
    # チーム曲（熱き星たちよ・チャンステーマ 等）は呼び方で決まり、歌詞も
    # 譜面も非公開の置き場にある。選手の公式歌詞は要らないので先に見る
    # 譜面がまだ届いていない時に選手の曲（「捕手のテーマ」等）へ当てない
    title = team_song.find(want)
    if title:
        if title not in team_song.titles(SHEET_DIR):
            raise LookupError("チーム曲の譜面がまだ無い")
        notes, tempo = await asyncio.to_thread(
            team_song.build, title, SHEET_DIR, moras)
        return notes, tempo, title
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
    sung_lines = (sheet_song.SUNG_LINES.get(key)
                  or sheet_song.drop_chant(sung_lines, key))
    sung_lines = [sheet_song.read_particles(x) for x in sung_lines]
    text = "".join(sung_lines) or text

    # 譜面（ゲームの応援歌エディタ）から起こした旋律があるならそれを使う。
    # 音源からの採譜は自前の音高追跡が入るぶん誤差が残る（実測 宮﨑: 楽譜に
    # オクターブの飛び値／楽譜 43 音に対し鳴った音 62）のに対し、譜面は高さと
    # 長さが離散値で書いてある。**ここに置いてあること自体が「読めた」の印**で、
    # 繰り返しの見立てが怪しい曲は置かない（判定を置き場で表す）
    if sheet_source(key):
        return await _prepare_sheet(key, songs)
    if not FROM_AUDIO:
        # 🔴 譜面の無い曲は歌わない。原曲の音源は歌の部分に旋律の楽器が無く
        # （伴奏とベースだけ）、音から採るとベースを拾って出鱈目な旋律になる
        # （user 実機 2026-11-06「筒香 適当な歌」「森敬斗 適当な歌」）
        log.info("%s は譜面がまだ無いので歌わない", key)
        raise LookupError("譜面がまだ無い")

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
    # 🔴 音源は掛け声込みの全曲ぶんある。**全歌詞で対応付けてから歌う範囲だけ
    # 切り出す**（歌詞だけ減らすと 1 音が伸びて「対応していない」で弾かれる。
    # 実測 2026-08-08: 筒香が 39 モーラ / 32 秒 = 0.83 秒で歌えなくなった）
    # 注意書き（※…）は音源で歌われていないので、対応付けにも入れない
    if key in SUNG_FROM_SEC:
        return await _prepare_two_parts(key, path, songs, sung_lines)
    all_lines = sheet_song.drop_notes(list(songs.get(key, [])))
    key_seg = key
    all_morae = moras("".join(all_lines))
    morae = moras(text)
    if not morae:
        raise LookupError("歌詞が読めない")
    head = 0
    for ln in all_lines:
        if ln in sung_lines:
            break
        head += len(moras(ln))
    tail = 0
    for ln in reversed(all_lines):
        if ln in sung_lines:
            break
        tail += len(moras(ln))
    if head or tail:
        notes, tempo = await notes_by_morae(key_seg, path, all_morae)
        end = len(notes) - tail if tail else len(notes)
        notes = notes[head:end]
        log.info("%s: 掛け声を除いて %d〜%d 音を歌う（全 %d モーラ）",
                 key, head, end, len(all_morae))
    else:
        notes, tempo = await notes_by_morae(key_seg, path, morae)
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
    for name in table if FROM_AUDIO else []:
        key = await asyncio.to_thread(server_tools._find_player,
                                      songs, name)
        if key and songs.get(key) and _cached_ok(name):
            out.append(key)
    for key, src in (_REUSE.items() if FROM_AUDIO else []):
        if songs.get(key) and match_player(table, src):
            out.append(key)
    for key in (songs if FROM_AUDIO else []):   # 手元に置いてもらった音源
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
