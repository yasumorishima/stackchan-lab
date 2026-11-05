"""チーム応援歌（熱き星たちよ・チャンステーマ 等）を譜面から歌う。

選手の応援歌と違い、名前ではなく**場面の呼び方**で頼まれる（「点が入った時の歌」
「負けてる時の歌」）。ここでは呼び方から曲を決めることと、譜面に歌詞を乗せる
ことだけをする。

歌詞と歌う範囲は非公開の置き場（`cache/sheets/team_songs.json`）に置く。
公開レポに歌詞を入れない決まりなので、ここに書くのは曲名と呼び方だけ。
JSON の中身: {曲名: {"sheet": 譜面のファイル名, "lines": [歌う行],
"from_bar": 歌い始める小節, "to_bar": 歌い終わる小節（含まない）}}。
パワプロの前奏（譜面の intro）は応援団の前奏＝演奏だけなので、
from_bar で外して歌わない。
"""
import json
import logging
import os
import random
import re

import sheet_song

log = logging.getLogger("team_song")

# 呼び方 → 曲名。**長い呼び方から順に当てる**（「チャンステーマ1」を
# 「チャンステーマ」より先に見る）。場面は公式の注記と yakyu-ouen.net の
# 一覧から（熱き星たちよ＝得点時・試合前後・ラッキー7、勝利の輝き＝攻撃
# 開始前、ライジングテーマ＝劣勢、横浜市歌＝四死球。譜面は四死球の版）
ALIASES = {
    "熱き星たちよ": ["熱き星たちよ", "熱き星", "あつきほし", "暑き星", "厚き星",
                   "得点", "点が入", "点入", "点を取", "点とった", "点取った",
                   "ラッキーセブン", "ラッキー7", "ラッキー７",
                   # 「横浜ベイスターズ歌って」を断っていた（実機 2026-11-05）。
                   # 歌い出しが「よこはまベイスターズ」の曲
                   "横浜ベイスターズ", "よこはまベイスターズ", "ベイスターズの歌",
                   "ベイスターズのうた", "球団の歌", "チームの歌"],
    "勝利の輝き": ["勝利の輝き", "しょうりのかがやき", "試合開始", "試合の始まり",
                 "試合が始まる", "プレイボール", "プレーボール"],
    "ライジングテーマ": ["ライジング", "負けてる", "負けている", "負けてい",
                     "劣勢", "リードされ"],
    "チャンステーマ0": ["チャンステーマ0", "チャンステーマ０", "チャンステーマゼロ",
                    "チャンステーマ零", "チャンステーマぜろ"],
    "チャンステーマ1": ["チャンステーマ1", "チャンステーマ１", "チャンステーマワン",
                    "チャンステーマ一", "チャンステーマいち", "ゲットザチャンス", "ゲッツザチャンス"],
    "チャンステーマ2": ["チャンステーマ2", "チャンステーマ２", "チャンステーマツー",
                    "チャンステーマ二"],
    "チャンステーマ3": ["チャンステーマ3", "チャンステーマ３", "チャンステーマスリー",
                    "チャンステーマ三", "チャンステーマさん", "ワッショイ", "わっしょい"],
    "ハンターチャンス": ["ハンターチャンス", "ハンター"],
    "攻めまくれ": ["攻めまくれ", "せめまくれ", "責めまくれ"],
    "Fight Oh! YOKOHAMA": ["ファイトオー", "ファイト横浜", "ファイトヨコハマ",
                           "fightoh", "fight"],
    "つなげ！": ["つなげ", "繋げ"],
    "All in one": ["オールインワン", "allinone", "マルチテーマ"],
    "BAY BLUE ANTHEM": ["ベイブルー", "bayblue", "アンセム"],
    "勇者の遺伝子": ["勇者の遺伝子", "勇者"],
    "横浜市歌": ["横浜市歌", "市歌", "四死球", "四球", "フォアボール", "デッドボール"],
}
_TEAM_NAMES = {"横浜ベイスターズ", "よこはまベイスターズ"}
# 「チャンステーマ」「チャンスの歌」とだけ言われた時の曲
DEFAULT_CHANCE = "チャンステーマ0"
_CHANCE_RE = re.compile(r"チャンス(?:テーマ|の歌|のうた|の時|のとき)")
# 頼み方（歌・テーマ・流して）。これが無い文では曲を選ばない（「得点圏」等）
CHANCES = ["チャンステーマ0", "チャンステーマ1", "チャンステーマ2",
           "チャンステーマ3", "ハンターチャンス"]
_OTHER_RE = re.compile(r"他の|ほかの|別の|べつの|違う|ちがう")
ASK_RE = re.compile(r"歌|うた|唄|テーマ|流して|聞かせて|聴かせて")


def _norm(text):
    t = re.sub(r"[\s　・！!？?、。「」『』\"'＂]", "", str(text or ""))
    return t.lower()


def find(text):
    """呼び方から曲名を返す。チーム曲の依頼でなければ None。"""
    t = _norm(text)
    if not t:
        return None
    pairs = sorted(((a, k) for k, al in ALIASES.items() for a in al),
                   key=lambda x: -len(x[0]))
    for alias, title in pairs:
        if alias in _TEAM_NAMES and "応援歌" in t:
            # 「横浜ベイスターズの牧の応援歌」は選手の歌
            continue
        if _norm(alias) in t:
            return title
    if _CHANCE_RE.search(t) or "チャンス" in t:
        if _OTHER_RE.search(t):
            # 「他のチャンステーマ」で同じ曲をまた歌っていた（実機 2026-11-05）
            return random.choice([c for c in CHANCES if c != DEFAULT_CHANCE])
        return DEFAULT_CHANCE
    return None


def request(text):
    """「得点の時の歌を歌って」のような依頼なら曲名、違えば None。"""
    if not ASK_RE.search(str(text or "")):
        return None
    return find(text)


def _table(sheet_dir):
    path = os.path.join(sheet_dir, "team_songs.json")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    return {k: v for k, v in d.items() if not k.startswith("_")}


def titles(sheet_dir):
    """歌える（歌詞と譜面の揃った）チーム曲の名前。"""
    out = []
    for title, spec in _table(sheet_dir).items():
        if os.path.exists(os.path.join(sheet_dir, spec["sheet"] + ".json")):
            out.append(title)
    return out


def joined(moras):
    """「+」でつないだ音を 1 つに数えるモーラ分け（べ+い → 「べい」1 つ）。

    応援では「ベイ」「スター」を 1 音で歌う。モーラ単位で数えると歌詞が
    音符より多くなり、1 つの音符に 2 つ詰めた所から後ろが全部ずれた
    （user 実機 2026-11-05「歌詞ズレすぎ」。ライジング 音符 86 に歌詞 96）。
    """
    def split(text):
        out = []
        for i, seg in enumerate(str(text).split("+")):
            ms = moras(seg)
            if i > 0 and out and ms:
                out[-1] += ms[0]
                ms = ms[1:]
            out.extend(ms)
        return out
    return split


def build(title, sheet_dir, moras):
    """(音符, テンポ) を返す。歌えなければ LookupError。"""
    spec = _table(sheet_dir).get(title)
    if not spec:
        raise LookupError("チーム曲の歌詞が無い")
    path = os.path.join(sheet_dir, spec["sheet"] + ".json")
    if not os.path.exists(path):
        raise LookupError("チーム曲の譜面が無い")
    with open(path, encoding="utf-8") as f:
        sheet = json.load(f)
    lo = int(spec.get("from_bar", 0)) * sheet_song.CELLS_PER_BAR
    hi = spec.get("to_bar")
    hi = int(hi) * sheet_song.CELLS_PER_BAR if hi is not None else None
    notes = [n for n in sheet["notes"]
             if n["t16"] >= lo and (hi is None or n["t16"] < hi)]
    if not notes:
        raise LookupError("歌う範囲に音符が無い")
    sheet = dict(sheet, notes=notes)
    try:
        out, tempo, nm, _call = sheet_song.build(
            sheet, spec["lines"], joined(moras), title, as_is=True)
    except Exception as e:
        log.warning("%s の譜面に歌詞が乗らない: %s: %s",
                    title, type(e).__name__, e)
        raise LookupError("譜面に歌詞が乗らない")
    log.info("%s: 譜面から歌う（音符 %d・歌詞 %d モーラ・テンポ %.0f）",
             title, len(notes), nm, tempo)
    return out, tempo
