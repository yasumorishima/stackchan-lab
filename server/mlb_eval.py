"""ask_mlb_stats の SQL 生成の正答率を測る（本番と同じ mlb_ask.ask を通す）。

正解: mlb_eval.json の gold SQL の 1 行目 1 列目。モデルの答えの 1 行目に
その値があれば正解（文字列は大小無視の一致、数値は相対 0.1% 以内、
率を 100 倍した値も可）。env は server.conf と同じもの。
Usage: python mlb_eval.py <runs>
"""
import json
import os
import sys
import time

import mlb_ask
import server_tools


def ok(expected, row):
    for v in row:
        if isinstance(expected, str):
            if isinstance(v, str) and v.strip().lower() == expected.lower():
                return True
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            for x in (v, v / 100):
                if abs(x - expected) <= 1e-3 * max(1.0, abs(expected)):
                    return True
    return False


def main(runs):
    base = os.environ.get("SAKURA_BASE", "https://api.ai.sakura.ad.jp")
    model, token = os.environ["SAKURA_MODEL"], os.environ["SAKURA_TOKEN"]
    cases = json.load(open("mlb_eval.json", encoding="utf-8"))
    only = os.environ.get("EVAL_SETS")
    if only:
        cases = [c for c in cases if c.get("set") in only.split(",")]
    con = mlb_ask.connection()
    n_req = [0]
    def count():
        n_req[0] += 1
        server_tools.count_llm_request()   # the same monthly counter as the voice server
    totals = []
    for r in range(runs):
        good = 0
        by_set = {}
        for i, c in enumerate(cases):
            exp = mlb_ask.run_sql(con, c["sql"])[1][0][0]
            t = time.time()
            try:
                con2 = mlb_ask.connection()
                text, sql = mlb_ask.ask(c["q"], base, model, token, on_request=count)
                cols, rows = mlb_ask.run_sql(con2, sql)
                hit = bool(rows) and ok(exp, rows[0])
            except Exception as e:  # noqa: BLE001
                sql, hit = f"ERROR {str(e)[:80]}", False
            good += hit
            k = c.get("set", "all")
            by_set[k] = by_set.get(k, 0) + hit
            print(f"run{r} q{i:02d} {'OK ' if hit else 'NG '} {time.time()-t:4.1f}s  exp={exp!r:.30}  sql={sql[:140]!r}", flush=True)
        totals.append(by_set)
        print(f"run{r}: {good}/{len(cases)} {by_set}", flush=True)
    print(json.dumps({"runs": totals, "n": len(cases), "requests": n_req[0]}))


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 1)
