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

CELLS_PER_BAR = 16
NAMES = "C C# D D# E F F# G G# A A# B".split()
BASE_KEY = 60                 # 相対の高さ 0 をここに置く（後で自動移調される）


def to_pitch(semi):
    k = BASE_KEY + semi
    return "%s%d" % (NAMES[k % 12], k // 12 - 1)


def share_morae(notes, morae):
    """モーラを音符に配る。まず 1 つずつ、余りは長い音符から 2 つ目を足す。"""
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
    return out


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


def build(sheet, lines, moras):
    """(音符, テンポ, モーラ数, コール行) を返す。

    sheet=譜面 JSON の dict / lines=公式歌詞の行 / moras=モーラ分割の関数
    （cheer_song.moras。ここで import すると循環になるので渡してもらう）。
    """
    sung_lines, call = split_call(list(lines))
    morae = moras("".join(sung_lines))
    notes = sorted(sheet["notes"], key=lambda n: n["t16"])
    notes = drop_strays(notes)
    if not notes or not morae:
        raise ValueError("譜面か歌詞が空")
    lyrics = share_morae(notes, morae)
    # 16 分音符 1 つの長さ = sec_per_bar/16。to_score は「長さ×60/tempo/4 秒」
    # なので tempo をそこから逆算する（1 小節 = 4 拍）
    tempo = 240.0 / sheet["sec_per_bar"]
    out, t = [], notes[0]["t16"]
    for n, ly in zip(notes, lyrics):
        if n["t16"] > t:
            out.append([None, n["t16"] - t, ""])          # 休み
        out.append([to_pitch(n["semi"]), n["len16"], ly])
        t = n["t16"] + n["len16"]
    return out, tempo, len(morae), call
