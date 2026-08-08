"""パワプロ譜面から起こした旋律（JSON）に公式の歌詞を乗せる。

音源が配布されていない選手（度会・石上・林・京田 等）は、応援歌エディタの
譜面動画から起こした旋律で歌う。読み取りは `~/pawapuro`（別リポジトリ）、
出来た JSON を `cache/sheets/<選手名>.json` に置くと cheer_song.prepare が
拾う（JSON はリポジトリに入れない＝音源・歌詞と同じ扱い）。

音源からの採譜（segment_dp）と違い、こちらは**ゲームの譜面そのもの**なので
高さと長さに読み違い以外の誤差が無い。歌詞は 1 音 1 モーラを基本に、
足りないぶんは長い音符から順に 2 モーラを割り当てる（応援歌は 16 分音符に
2 音が乗ることがある）。

⚠️ 曲中の休みを縮めるのは**やらない**。譜面には音の長さがそのまま書いて
あるのだから、勝手に詰めるとリズムが譜面と違うものになる（声の出ている
割合という数字を上げるために、肝心のリズムを崩した実績がある）。声が出て
いない時間が長いなら、それは音符の読み落としなので読みの側を直す。

コールは**音として付けない**（user 指示 2026-08-07「これはなくていいよ、
歌じゃないし」）。歌わせるのも読み上げるのも無し。歌い終わりで終わる。
"""

import re

CELLS_PER_BAR = 16
NAMES = "C C# D D# E F F# G G# A A# B".split()
BASE_KEY = 60                 # 相対の高さ 0 をここに置く（後で自動移調される）


def to_pitch(semi):
    k = BASE_KEY + semi
    return "%s%d" % (NAMES[k % 12], k // 12 - 1)


def share_morae(notes, morae):
    """モーラを音符に配る（全体一括の後詰め。フレーズ対応が取れない時の控え）。

    まず 1 つずつ、余りは長い音符から 2 つ目を足す。音符の方が多ければ
    短い音符から前の音とつなげる。**併合した音符の並びも返す**（返さないと
    呼び出し側が元の並びと zip して、末尾の音符が黙って落ちる＝実測 京田の
    最終音が消えていた）。
    """
    if len(morae) < len(notes):
        # 音符の方が多いぶんは、**短い音符から順に前の音とつなげる**（同じ高さの
        # 打ち直しを 1 つに戻す）。読み違いで切れ目が増えたぶんを吸収する
        merged, extra = list(notes), len(notes) - len(morae)
        while extra > 0 and len(merged) > 1:
            i = min(range(1, len(merged)), key=lambda i: merged[i]["len16"])
            prev = merged[i - 1]
            prev = dict(prev,
                        len16=merged[i]["t16"] + merged[i]["len16"] - prev["t16"])
            merged[i - 1:i + 1] = [prev]
            extra -= 1
        notes = merged
    per = [1] * len(notes)
    extra = len(morae) - len(notes)
    for i in sorted(range(len(notes)), key=lambda i: -notes[i]["len16"]):
        if extra <= 0:
            break
        per[i] += 1
        extra -= 1
    if extra > 0:
        # 1 音は最大 2 モーラ。それでも配りきれないのは譜面の読み落としが
        # 大きい（歌詞がモーラ数で音符の 2 倍超）ということ＝黙って歌詞の
        # 末尾を捨てると気付けないので、歌わない側に倒す（監査指摘 1a）
        raise ValueError("歌詞が音符に乗り切らない（モーラ %d / 音符 %d）"
                         % (len(morae), len(notes)))
    out, k = [], 0
    for n, c in zip(notes, per):
        out.append("".join(morae[k:k + c]))
        k += c
    return notes, out


# 伸ばし用の母音（「つないでー」の ー を歌わせるための、直前のモーラの母音）
_VOWEL_ROWS = (("あ", "あかさたなはまやらわがざだばぱぁゃ"),
               ("い", "いきしちにひみりぎじぢびぴぃ"),
               ("う", "うくすつぬふむゆるぐずづぶぷゔぅゅ"),
               ("え", "えけせてねへめれげぜでべぺぇ"),
               ("お", "おこそとのほもよろをごぞどぼぽぉょ"))
_VOWEL = {k: v for v, ks in _VOWEL_ROWS for k in ks}


def _extension(mora):
    """モーラの伸ばし（母音）。カタカナは平仮名に寄せてから引く。"""
    c = mora[-1]
    if "ァ" <= c <= "ヶ":
        c = chr(ord(c) - 0x60)
    return _VOWEL.get(c, c)


MIN_PHRASE_NOTES = 3      # これ未満のフレーズは歌詞 1 行が乗らないので併合する


def _merge_short(out):
    """音符が少なすぎるフレーズを隣へくっつける。

    休符で切ると音符 1〜2 個の塊ができることがある（実測 柴田 [9,1,1,29] /
    石上 [2,2,9,10,6]）。歌詞 1 行ぶんが乗らない大きさなので、そこへ配ると
    「ミー / トと」のように語の途中で割れる。**短い方の隣へ寄せる**。
    譜面の音そのものは触らない（配り方だけの話）。
    """
    while len(out) > 1:
        i = min(range(len(out)), key=lambda k: len(out[k]))
        if len(out[i]) >= MIN_PHRASE_NOTES:
            break
        if i == 0:
            j = 1
        elif i == len(out) - 1:
            j = i - 1
        else:
            j = i - 1 if len(out[i - 1]) <= len(out[i + 1]) else i + 1
        lo, hi = min(i, j), max(i, j)
        out[lo:hi + 1] = [out[lo] + out[hi]]
    return out


def _phrases(notes):
    """休符（t16 の切れ目）で音符の並びをフレーズに分ける。"""
    out, cur, end = [], [], None
    for n in notes:
        if cur and n["t16"] > end:
            out.append(cur)
            cur = []
        cur.append(n)
        end = n["t16"] + n["len16"]
    if cur:
        out.append(cur)
    return _merge_short(out)


def _fit_words(words, phrases):
    """歌詞のモーラ列をフレーズへ配る切り方を DP で選ぶ。

    切り口はモーラ単位（歌詞の行に空白が無い曲もある＝実測 石上は行内
    無空白で、語単位の DP だと語 4 に対しフレーズ 5 で不成立だった）。
    語（空白・行の区切り）の境目で切れる分け方を少し好む。採点は
    「フレーズのモーラ数と音符数のずれの二乗和＋語境界ペナルティ」。
    1 音に 3 モーラ以上は歌えないので、そうなる分け方は選ばない。
    """
    morae = [m for w in words for m in w]
    bounds, acc = set(), 0
    for w in words:
        acc += len(w)
        bounds.add(acc)
    m_n, p_n = len(morae), len(phrases)
    if m_n == 0 or p_n == 0:
        return None
    # 🔴 まず「語の切れ目でしか切らない」で解く。語の途中で切ると
    # 「と / た / くみな」のような中途半端な歌になる（user 指摘 2026-08-08）
    got = _solve_fit(morae, phrases, bounds, only_bounds=True)
    if got is not None:
        return got
    return _solve_fit(morae, phrases, bounds, only_bounds=False)


def _solve_fit(morae, phrases, bounds, only_bounds):
    """`only_bounds` なら語の切れ目だけで切る。解けなければ None。"""
    m_n, p_n = len(morae), len(phrases)
    inf = float("inf")
    cost = [[inf] * (p_n + 1) for _ in range(m_n + 1)]
    back = [[0] * (p_n + 1) for _ in range(m_n + 1)]
    cost[0][0] = 0.0
    for p in range(1, p_n + 1):
        n_p = len(phrases[p - 1])
        for e in range(1, m_n + 1):
            for s in range(0, e):
                if cost[s][p - 1] == inf:
                    continue
                m = e - s
                if m > 2 * n_p:
                    continue
                at_bound = e in bounds or e == m_n
                if only_bounds and not at_bound:
                    continue
                # 語境界を外す罰は大きくする（0.3 ではモーラ数のずれの
                # 二乗に負けて、語の途中で平気で切っていた）
                c = (cost[s][p - 1] + (m - n_p) ** 2
                     + (0.0 if at_bound else 6.0))
                if c < cost[e][p]:
                    cost[e][p] = c
                    back[e][p] = s
    if cost[m_n][p_n] == inf:
        return None
    cuts, e = [], m_n
    for p in range(p_n, 0, -1):
        cuts.append((back[e][p], e))
        e = back[e][p]
    return [morae[a:b] for a, b in reversed(cuts)]


def _share_phrase(notes, morae):
    """1 フレーズぶんの配り（音符ごとのモーラの列を返す）。

    1 音 1 モーラが基本、足りなければ長い音符に 2 つ、音符が余れば
    **末尾の音符は直前のモーラの母音で伸ばす**（「つないでー」の ー。
    途中の音符を勝手に併合しない）。"""
    n, m = len(notes), len(morae)
    if m > n:
        per = [1] * n
        extra = m - n
        for i in sorted(range(n), key=lambda i: -notes[i]["len16"]):
            if extra <= 0:
                break
            per[i] += 1
            extra -= 1
        if extra > 0:
            raise ValueError("歌詞が音符に乗り切らない（モーラ %d / 音符 %d）"
                             % (m, n))
        out, k = [], 0
        for c in per:
            out.append(morae[k:k + c])
            k += c
        return out
    return [[x] for x in morae] + [[_extension(morae[-1])]] * (n - m)


# 掛け声だけの行（「オオオオー」等）。音程を持たないので旋律に乗せない
_CHANT_RE = re.compile(r"^[オぉおー\s！!・]*$")
# 🔴 歌の前の掛け声が「普通の歌詞に見える」曲がある。文字では判別できないので
# **曲ごとの事実として持つ**（値 = 歌が始まる行番号）。user 様の指摘で埋める。
# 牧: 「オオオオー」3 行に加えて「とどけ われらのこえ」も掛け声（2026-08-08）
LEAD_CHANT = {"牧秀悟": 4}


def drop_chant(lines, name=""):
    """先頭に続く掛け声の行を落とす。

    公式歌詞の先頭に観客の掛け声が入ることがある（牧: 「オオオオー オオオオー」
    「オオオオーオオ マキシュウゴ！」「オオオオオ オオオオオ」＝29 モーラ）。
    ここに旋律を割り当てると歌にならない（実機 2026-08-08「めちゃくちゃ」）。

    **先頭から続く分だけ**落とす（曲中の「オオオオ」は歌詞の一部なので残す）。
    選手名がカタカナで混ざる行も掛け声とみなす。
    """
    lead = LEAD_CHANT.get(name)
    if lead:
        return list(lines[lead:]) or list(lines)
    out = list(lines)
    while out:
        t = re.sub(r"[ァ-ヿ]{3,}", "", out[0])
        if not _CHANT_RE.match(t):
            break
        out.pop(0)
    return out if out else list(lines)


def split_call(lines):
    """歌う行と、最後のコール（叫び）を分ける。

    応援歌の最後の「かっとばせー！○○！」は**歌ではなく叫び**で、譜面にも
    音符が無い（実測: 度会の譜面は最後が長い音 1 つで終わり、コールぶんの
    音符は書かれていない）。ここを旋律に乗せると、余った音符に無理やり
    詰め込まれて歌にならない（user 指摘 2026-08-07）。
    """
    if lines and ("かっとばせ" in lines[-1] or "かっ飛ばせ" in lines[-1]):
        return lines[:-1], lines[-1]
    return lines, ""


def drop_strays(notes):
    """前奏側に 1 つだけ離れて出た誤検出を落とす。

    実測: 度会の 1 小節目に「1 マスだけの音」が 1 つ出ており、そこから曲が
    始まっていることになって歌詞の割り当てが 1 小節ぶんずれた（読みの側は
    直したが、防波堤として残す）。
    """
    if len(notes) < 4:
        return notes
    while len(notes) > 1 and notes[0]["len16"] <= 1 and \
            notes[1]["t16"] - notes[0]["t16"] >= CELLS_PER_BAR:
        notes = notes[1:]
    return notes


def build(sheet, lines, moras, name=""):
    """(音符, テンポ, モーラ数, コール行) を返す。

    sheet=譜面 JSON の dict / lines=公式歌詞の行 / moras=モーラ分割の関数
    （cheer_song.moras。ここで import すると循環になるので渡してもらう）。

    配りは**フレーズ単位**が基本: 旋律を休符で区切り、歌詞を空白区切りの
    語で区切り、語の組をフレーズへ DP で対応付ける。全体一括の後詰めだと
    余り・不足の調整が曲のどこで起きるか制御できず、離れた場所の 1 音の
    ずれが伸ばす音節を狂わせる（実測 度会: 最後の「ためにー」が
    「ためーに」になり user 指摘。フレーズ単位なら 18 音 18 モーラの
    1:1 で自動的に合う）。
    """
    sung_lines, call = split_call(list(lines))
    # 先頭の掛け声（オオオオー…）は音程を持たないので歌わない
    sung_lines = drop_chant(sung_lines, name)
    morae = moras("".join(sung_lines))
    notes = sorted(sheet["notes"], key=lambda n: n["t16"])
    notes = drop_strays(notes)
    if not notes or not morae:
        raise ValueError("譜面か歌詞が空")
    words = [moras(w) for ln in sung_lines for w in ln.split()]
    words = [w for w in words if w]
    phrases = _phrases(notes)
    groups = _fit_words(words, phrases) if len(phrases) >= 2 else None
    if groups is not None:
        lyrics = []
        for ph, gm in zip(phrases, groups):
            lyrics.extend(_share_phrase(ph, gm))
    else:
        notes, joined = share_morae(notes, morae)
        lyrics = [[x] for x in joined]
    # 16 分音符 1 つの長さ = sec_per_bar/16。to_score は「長さ×60/tempo/4 秒」
    # なので tempo をそこから逆算する（1 小節 = 4 拍）
    tempo = 240.0 / sheet["sec_per_bar"]
    out, t = [], notes[0]["t16"]
    for n, ms in zip(notes, lyrics):
        if n["t16"] > t:
            out.append([None, n["t16"] - t, ""])          # 休み
        pitch = to_pitch(n["semi"])
        if len(ms) > 1 and n["len16"] >= len(ms) + 1:
            # 詰め込みの 2 モーラは「先を短く・後を残り全部で伸ばす」。
            # 1 音のまま渡すと均等割り（sing_vv._spread）になり、
            # 「ためにー」が「ためー・にー」に化ける（user 指摘 2026-08-08）
            lead = 1 if n["len16"] < 4 else 2
            rest = n["len16"] - lead * (len(ms) - 1)
            for m in ms[:-1]:
                out.append([pitch, lead, m])
            out.append([pitch, rest, ms[-1]])
        else:
            out.append([pitch, n["len16"], "".join(ms)])
        t = n["t16"] + n["len16"]
    return out, tempo, len(morae), call
