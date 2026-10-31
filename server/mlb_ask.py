"""MLB のシーズン成績に日本語で答える（ask_mlb_stats の中身）。

データは mlb-data-pipeline が Hugging Face に公開している dbt のマート
（marts/*.parquet）。手元の cache/marts/ に写し、manifest の commit が
変わったときだけ取り直す（確認は 1 日 1 回まで）。

質問 -> さくらの LLM が DuckDB の SELECT を 1 文書く -> 検査して実行 ->
結果を短い表にして返す。安全のための決まり:
  * 接続はメモリ上の DuckDB。マートを読み込んだあと外部アクセスを切り、
    設定を固定する（ファイル読み書き・拡張の読み込み・設定変更ができない）
  * 実行するのは SELECT（集合演算を含む）1 文だけ。構文木で確かめる
  * 行数は 20 行まで、実行は 5 秒で打ち切る
  * 失敗したら、エラー文を添えて 1 回だけ書き直させる
"""
import json
import os
import pathlib
import re
import threading
import time
import urllib.request

import duckdb

HERE = pathlib.Path(__file__).resolve().parent
CACHE = HERE / "cache" / "marts"
HF = "https://huggingface.co/datasets/yasumorishima/mlb-stats/resolve/main/marts/"
SCHEMA = json.loads((HERE / "mlb_schema.json").read_text(encoding="utf-8"))
MAX_ROWS = 20
TIMEOUT_S = 5.0
CHECK_EVERY_S = 86400

_lock = threading.Lock()
_con = None
_checked = 0.0


def _get(url, timeout=60):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def refresh(force=False):
    """manifest が変わっていれば parquet を取り直す。取れなければ手元のまま。"""
    global _checked
    if not force and time.time() - _checked < CHECK_EVERY_S and (CACHE / "_manifest.json").exists():
        return False
    _checked = time.time()
    CACHE.mkdir(parents=True, exist_ok=True)
    new = json.loads(_get(HF + "_manifest.json", 30))
    old_p = CACHE / "_manifest.json"
    old = json.loads(old_p.read_text()) if old_p.exists() else {}
    if old.get("pipeline_commit") == new.get("pipeline_commit") and \
            old.get("input_dataset_revision") == new.get("input_dataset_revision") and \
            all((CACHE / f"{t}.parquet").exists() for t in SCHEMA):
        return False
    for t in SCHEMA:
        body = _get(HF + f"{t}.parquet")
        tmp = CACHE / f"{t}.parquet.tmp"
        tmp.write_bytes(body)
        os.replace(tmp, CACHE / f"{t}.parquet")
    old_p.write_text(json.dumps(new))
    return True


def _connect():
    con = duckdb.connect(":memory:")
    for t in SCHEMA:
        p = (CACHE / f"{t}.parquet").as_posix().replace("'", "''")
        con.execute(f"create table {t} as select * from read_parquet('{p}')")
    con.execute("set enable_external_access = false")
    con.execute("set lock_configuration = true")
    return con


def connection():
    global _con
    with _lock:
        try:
            changed = refresh()
        except Exception:  # 取れないときは手元のデータで答える
            changed = False
        if _con is None or changed:
            _con = _connect()
        return _con


def schema_text():
    lines = []
    for t, s in SCHEMA.items():
        lines.append(f"table {t}: {s['description']}")
        for name, typ, desc in s["columns"]:
            lines.append(f"  {name} {typ}" + (f" -- {desc}" if desc else ""))
    return "\n".join(lines)


SYSTEM = """あなたは MLB のシーズン成績データベースに対する DuckDB の SQL を書く。
質問に答える SELECT 文を 1 つだけ、```sql ... ``` の中に書く。説明は書かない。

決まり:
- 使えるのは下の表だけ。関数で外部のファイルや URL を読まない。
- 選手名は英語表記（例: Shohei Ohtani, Aaron Judge）。日本語で言われた名前は英語に直し、
  姓で strip_accents(player_name) ilike '%ohtani%' のように探す（Ramírez・Acuña など
  アクセント付きの名前があるので、必ず strip_accents を通す）。
- 率は 0〜1 の小数（k_rate 0.25 は 25%）。wOBA は .300 前後の小数。
- 年を言われたらその年の season をそのまま使う。「今年」「最新」はデータにある最大の season。「去年」はその 1 つ前。
  season の最大値のシーズンは進行中のことがある（is_partial が true）。
- 打者は mart_batter_season、投手は mart_pitcher_season、球種は
  mart_pitch_arsenal_scouting、指標の翌年への持ち越しは mart_scouting_reliability、
  同じ打者の連続 2 シーズンは mart_batter_aging_pairs。
- 投手の成績（防御率 era・FIP・xERA・奪三振率 k_rate・与四球率 bb_rate・K-BB% k_minus_bb_rate・
  投球回 ip）は必ず mart_pitcher_season から取る。投手も打席に立つので mart_batter_season にも
  名前があるが、そこの k_rate は打者としての三振率。打者の成績（wOBA・wRC+・出塁率・
  打席 pa・スプリントスピード・バットスピード）は mart_batter_season。
- 球種は pitch_type の略号で絞る: FF=フォーシーム, SL=スライダー, SI=シンカー, CH=チェンジアップ,
  CU=カーブ, FC=カッター, ST=スイーパー, FS=スプリット, SV=スラーブ, KN=ナックル, FO=フォーク。
  mart_pitch_arsenal_scouting に名前は無いので、名前は mart_pitcher_season と
  player_id, season で結合して取る。
- mart_scouting_reliability を使うときは、聞かれた指標で metric を必ず絞る
  （usage, whiff_rate, run_value_per_100, xwoba_allowed, hard_hit_rate）。
- ランキングは規定の目安で絞る（打者 pa >= 300、投手 ip >= 100 など）ことを検討する。
- 結果は 20 行以内になるようにする。答えに必要な列（名前・season・値）を選ぶ。

日本人選手の表記（データにいる選手。player_name はこの英語表記。投=投手・打=打者）:
大谷翔平=Shohei Ohtani(打・投), ダルビッシュ有=Yu Darvish(投), 山本由伸=Yoshinobu Yamamoto(投),
今永昇太=Shota Imanaga(投), 千賀滉大=Kodai Senga(投), 鈴木誠也=Seiya Suzuki(打),
吉田正尚=Masataka Yoshida(打), 前田健太=Kenta Maeda(投), 菊池雄星=Yusei Kikuchi(投),
松井裕樹=Yuki Matsui(投), 佐々木朗希=Roki Sasaki(投), 田中将大=Masahiro Tanaka(投),
筒香嘉智=Yoshi Tsutsugo(打), 秋山翔吾=Shogo Akiyama(打), 澤村拓一=Hirokazu Sawamura(投),
有原航平=Kohei Arihara(投), 平野佳寿=Yoshihisa Hirano(投), 牧田和久=Kazuhisa Makita(投),
岩隈久志=Hisashi Iwakuma(投), 上原浩治=Koji Uehara(投), 川﨑宗則=Munenori Kawasaki(打),
藤浪晋太郎=Shintaro Fujinami(投), 菅野智之=Tomoyuki Sugano(投), 小笠原慎之介=Shinnosuke Ogasawara(投),
村上宗隆=Munetaka Murakami(打), 今井達也=Tatsuya Imai(投), 岡本和真=Kazuma Okamoto(打),
上沢直之=Naoyuki Uwasawa(投), 田澤純一=Junichi Tazawa(投), イチロー=Ichiro Suzuki(打)

表:
"""


def extract_sql(text):
    m = re.search(r"```(?:sql)?\s*(.*?)```", text, re.S | re.I)
    sql = (m.group(1) if m else text).strip().rstrip(";").strip()
    return sql


def check_select(con, sql):
    """SELECT 1 文でなければ ValueError。"""
    tree = json.loads(con.execute("select json_serialize_sql(?)", [sql]).fetchone()[0])
    if tree.get("error"):
        raise ValueError(tree.get("error_message", "parse error"))
    stmts = tree.get("statements", [])
    if len(stmts) != 1:
        raise ValueError(f"{len(stmts)} statements")
    kind = stmts[0]["node"]["type"]
    if kind not in ("SELECT_NODE", "SET_OPERATION_NODE", "CTE_NODE"):
        raise ValueError(f"not a SELECT: {kind}")


def run_sql(con, sql):
    check_select(con, sql)
    res, err = {}, {}
    cur = con.cursor()   # interrupt() must go to the handle running the query

    def work():
        try:
            cur.execute(f"select * from ({sql}) limit {MAX_ROWS + 1}")
            res["cols"] = [d[0] for d in cur.description]
            res["rows"] = cur.fetchall()
        except Exception as e:  # noqa: BLE001
            err["e"] = e

    th = threading.Thread(target=work, daemon=True)
    th.start()
    th.join(TIMEOUT_S)
    if th.is_alive():
        cur.interrupt()
        th.join(5)
        if th.is_alive():
            raise RuntimeError("query did not stop after interrupt")
        raise TimeoutError("query took too long")
    if "e" in err:
        raise err["e"]
    return res["cols"], res["rows"]


# 日本人選手は、答えを読み上げるときに漢字の読みを LLM に推測させない（2026-09-26、
# 山本由伸が「ヤマモト ユウシン」と読まれた）。読みは英語表記のローマ字から起こした。
JA_READING = {
    "Shohei Ohtani": "おおたに しょうへい", "Yu Darvish": "ダルビッシュ ゆう",
    "Yoshinobu Yamamoto": "やまもと よしのぶ", "Shota Imanaga": "いまなが しょうた",
    "Kodai Senga": "せんが こうだい", "Seiya Suzuki": "すずき せいや",
    "Masataka Yoshida": "よしだ まさたか", "Kenta Maeda": "まえだ けんた",
    "Yusei Kikuchi": "きくち ゆうせい", "Yuki Matsui": "まつい ゆうき",
    "Roki Sasaki": "ささき ろうき", "Masahiro Tanaka": "たなか まさひろ",
    "Yoshi Tsutsugo": "つつごう よしとも", "Shogo Akiyama": "あきやま しょうご",
    "Hirokazu Sawamura": "さわむら ひろかず", "Kohei Arihara": "ありはら こうへい",
    "Yoshihisa Hirano": "ひらの よしひさ", "Kazuhisa Makita": "まきた かずひさ",
    "Hisashi Iwakuma": "いわくま ひさし", "Koji Uehara": "うえはら こうじ",
    "Munenori Kawasaki": "かわさき むねのり", "Shintaro Fujinami": "ふじなみ しんたろう",
    "Tomoyuki Sugano": "すがの ともゆき", "Shinnosuke Ogasawara": "おがさわら しんのすけ",
    "Munetaka Murakami": "むらかみ むねたか", "Tatsuya Imai": "いまい たつや",
    "Kazuma Okamoto": "おかもと かずま", "Naoyuki Uwasawa": "うわさわ なおゆき",
    "Junichi Tazawa": "たざわ じゅんいち", "Ichiro Suzuki": "イチロー",
}


def fmt_value(v):
    if isinstance(v, str) and v in JA_READING:
        return JA_READING[v]
    if isinstance(v, float) and v.is_integer() and abs(v) >= 10:
        return str(int(v))
    if isinstance(v, float):
        return f"{v:.3f}".rstrip("0").rstrip(".") if abs(v) < 10 else f"{v:.1f}"
    return str(v)


def format_rows(cols, rows):
    if not rows:
        return "該当するデータはありませんでした。"
    more = len(rows) > MAX_ROWS
    lines = [", ".join(f"{c}={fmt_value(v)}" for c, v in zip(cols, r)) for r in rows[:MAX_ROWS]]
    return "\n".join(lines) + ("\n（ほかにもあります）" if more else "")


def chat(base, model, token, messages, timeout=60):
    req = urllib.request.Request(
        base + "/v1/chat/completions",
        data=json.dumps({"model": model, "messages": messages, "temperature": 0}).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())["choices"][0]["message"]["content"] or ""


def ask(question, base, model, token, on_request=None):
    """質問に答える。戻り値 (答えの表, 実行した SQL)。"""
    con = connection()
    msgs = [{"role": "system", "content": SYSTEM + schema_text()},
            {"role": "user", "content": question}]
    last_err = None
    for attempt in range(2):  # the first try and one retry
        text = chat(base, model, token, msgs)
        if on_request:
            on_request()
        sql = extract_sql(text)
        try:
            cols, rows = run_sql(con, sql)
            if rows or attempt == 1:
                return format_rows(cols, rows), sql
            last_err = "0 rows"
            msgs += [{"role": "assistant", "content": text},
                     {"role": "user", "content": "その SQL の結果は 0 行でした。名前の表記や条件が違う可能性があります。直した SELECT を 1 つだけ書いてください。"}]
            continue
        except Exception as e:  # noqa: BLE001
            last_err = e
            msgs += [{"role": "assistant", "content": text},
                     {"role": "user", "content": f"その SQL はエラーになりました: {str(e)[:300]}\n直した SELECT を 1 つだけ書いてください。"}]
    raise RuntimeError(f"SQL を作れませんでした: {last_err}")
