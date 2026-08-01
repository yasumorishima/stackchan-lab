"""サーバー側ツール（server_tools）の単体試験。

実際に Open-Meteo を叩く経路と、取得先が落ちている時の退避を両方通す。
本体も LLM も要らないので、これだけは常に実行できる。

  ./.venv/bin/python test_tools.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import aiohttp

import server_tools

CASES = [
    ({}, "既定の場所・今日"),
    ({"when": "now"}, "現在の実況"),
    ({"place": "東京", "when": "tomorrow"}, "地名＋明日"),
    ({"place": "北海道"}, "都道府県名で引く"),
    ({"place": "Kobe", "when": "day_after_tomorrow"}, "ローマ字＋明後日"),
    ({"place": "鳥取"}, "同名の小地名でなく県庁所在地を選ぶ"),
    ({"place": "ぬるぽ市"}, "知らない地名は素直に分からないと言う"),
]


async def main():
    ok = True
    async with aiohttp.ClientSession() as s:
        for args, memo in CASES:
            text = await server_tools.call(s, "get_weather", args)
            print("%-28s %s" % (memo, text))
            bad = text.startswith("error:")
            if memo.startswith("同名") and "鳥取市" not in text:
                ok = False
                print("   ^ 県庁所在地が選ばれていない")
            if bad != (memo.startswith("知らない")):
                ok = False
                print("   ^ 想定と違う")

        # 取得先が落ちている場合: キャッシュが無ければ穏当な文言、あれば古い値へ退避
        real = server_tools.FORECAST_URL
        server_tools.FORECAST_URL = "http://127.0.0.1:9/forecast"
        server_tools._forecast_cache.clear()
        text = await server_tools.call(s, "get_weather", {})
        print("%-28s %s" % ("取得先が死んでいる", text))
        if "調べられませんでした" not in text:
            ok = False
        server_tools._forecast_cache[(35.4333, 139.65)] = (
            server_tools.time.monotonic() - 3600,
            {"current": {"temperature_2m": 20.0, "weather_code": 3,
                         "wind_speed_10m": 1.0, "relative_humidity_2m": 50},
             "daily": {"time": ["2026-07-28"], "weather_code": [3],
                       "temperature_2m_max": [30.0], "temperature_2m_min": [20.0],
                       "precipitation_probability_max": [10]}})
        text = await server_tools.call(s, "get_weather", {})
        print("%-28s %s" % ("死んでいるが古い値あり", text))
        if "分前の情報" not in text:
            ok = False
        # 古すぎる値は使わない
        server_tools._forecast_cache[(35.4333, 139.65)] = (
            server_tools.time.monotonic() - server_tools.STALE_MAX - 60,
            server_tools._forecast_cache[(35.4333, 139.65)][1])
        text = await server_tools.call(s, "get_weather", {})
        print("%-28s %s" % ("古すぎる値は使わない", text))
        if "調べられませんでした" not in text:
            ok = False

        # API が値に null を返しても書式化で落ちない
        server_tools._forecast_cache[(35.4333, 139.65)] = (
            server_tools.time.monotonic(),
            {"current": {"temperature_2m": None, "weather_code": None,
                         "wind_speed_10m": None, "relative_humidity_2m": None},
             "daily": {"time": ["2026-07-28"], "weather_code": [None],
                       "temperature_2m_max": [None], "temperature_2m_min": [None],
                       "precipitation_probability_max": [None]}})
        text = await server_tools.call(s, "get_weather", {})
        print("%-28s %s" % ("値が null なら取れないと言う", text))
        # 0 で埋めて「最高 0.0 度」と読み上げないこと
        if not text.startswith("error:") or "0.0度" in text:
            ok = False

        # 長すぎる引数を投げられても素通しにしない
        text = await server_tools.call(s, "get_weather", {"place": "あ" * 5000})
        print("%-28s %s" % ("長すぎる地名", text[:60]))
        if not text.startswith("error:"):
            ok = False

        server_tools.FORECAST_URL = real

        # ---- ドル円 ----
        # 実際に取りに行く（主か予備のどちらかが生きていれば通る）
        text = await server_tools.call(s, "get_usdjpy", {})
        print("%-28s %s" % ("ドル円を実取得", text))
        if text.startswith("error:") or "円" not in text:
            ok = False

        # 取得先が全部死んでいる: 新しいキャッシュがあればそのまま読む
        real_urls = (server_tools.RATE_URL_YAHOO, server_tools.RATE_URL_COINBASE,
                     server_tools.RATE_URL_DAILY)
        server_tools.RATE_URL_YAHOO = "http://127.0.0.1:9/chart"
        server_tools.RATE_URL_COINBASE = "http://127.0.0.1:9/rates"
        server_tools.RATE_URL_DAILY = "http://127.0.0.1:9/latest"
        text = await server_tools.call(s, "get_usdjpy", {})
        print("%-28s %s" % ("死んでいるが直前の値あり", text))
        if text.startswith("error:"):
            ok = False

        # 古い（15分超）キャッシュへ退避したら古さを言う
        server_tools._rate_cache[:] = [(
            server_tools.time.monotonic() - 1800,
            server_tools._rate_cache[0][1], server_tools._rate_cache[0][2])]
        text = await server_tools.call(s, "get_usdjpy", {})
        print("%-28s %s" % ("死んでいるが古い値あり", text))
        if "分前の情報" not in text:
            ok = False

        # 古すぎる値は使わない
        server_tools._rate_cache[:] = [(
            server_tools.time.monotonic() - server_tools.RATE_STALE_MAX - 60,
            150.0, "リアルタイム")]
        text = await server_tools.call(s, "get_usdjpy", {})
        print("%-28s %s" % ("古すぎる値は使わない", text))
        if "調べられませんでした" not in text:
            ok = False

        server_tools._rate_cache.clear()
        (server_tools.RATE_URL_YAHOO, server_tools.RATE_URL_COINBASE,
         server_tools.RATE_URL_DAILY) = real_urls

        # ---- 株価指数 ----
        # 実際に取りに行く（index 指定なし＝3 指数まとめて）
        text = await server_tools.call(s, "get_stock_index", {})
        print("%-28s %s" % ("株価指数を実取得", text))
        if (text.startswith("error:") or "日経平均株価" not in text
                or "エスアンドピー500" not in text):
            ok = False

        # index 指定なら 1 つだけ
        text = await server_tools.call(s, "get_stock_index", {"index": "nikkei"})
        print("%-28s %s" % ("日経平均だけ", text))
        if text.startswith("error:") or "ダウ" in text:
            ok = False

        # 知らない index は素通しにせず全指数で答える
        text = await server_tools.call(s, "get_stock_index", {"index": "ぬるぽ"})
        print("%-28s %s" % ("知らない index は全部", text))
        if "日経平均株価" not in text:
            ok = False

        # 取得先が全部死んでいる: 新しいキャッシュがあればそのまま読む
        real_hosts = server_tools.STOCK_HOSTS
        server_tools.STOCK_HOSTS = ("127.0.0.1:9",)
        text = await server_tools.call(s, "get_stock_index", {"index": "nikkei"})
        print("%-28s %s" % ("死んでいるが直前の値あり", text))
        if text.startswith("error:") or "分前の情報" in text:
            ok = False

        # 古い（15分超）キャッシュへ退避したら古さを言う
        _ts, _line = server_tools._stock_cache["nikkei"]
        server_tools._stock_cache["nikkei"] = (
            server_tools.time.monotonic() - 1800, _line)
        text = await server_tools.call(s, "get_stock_index", {"index": "nikkei"})
        print("%-28s %s" % ("死んでいるが古い値あり", text))
        if "分前の情報" not in text:
            ok = False

        # 古すぎる値は使わない
        server_tools._stock_cache["nikkei"] = (
            server_tools.time.monotonic() - server_tools.STOCK_STALE_MAX - 60, _line)
        text = await server_tools.call(s, "get_stock_index", {"index": "nikkei"})
        print("%-28s %s" % ("古すぎる値は使わない", text))
        if "調べられませんでした" not in text:
            ok = False

        server_tools._stock_cache.clear()
        server_tools.STOCK_HOSTS = real_hosts

        # 読み上げ書式（通信なし）。.5 は使わない（round は偶数丸め）
        POINTS = [((64362.02, "円"), "64362円"), ((6300.35, "ポイント"), "6300ポイント"),
                  ((44901.92, "ドル"), "44902ドル")]
        for (v, u), want in POINTS:
            got = server_tools._fmt_points(v, u)
            mark = "OK" if got == want else "NG"
            if got != want:
                ok = False
            print("%-4s _fmt_points(%s) -> %s (期待 %s)" % (mark, v, got, want))

    # 値の門番と読み上げ書式（通信なし）
    SANE = [(147.25, 147.25), (0, None), ("abc", None), (None, None),
            (1e9, None), (49.9, None), (50.0, 50.0)]
    for v, want in SANE:
        got = server_tools._sane_rate(v)
        mark = "OK" if got == want else "NG"
        if got != want:
            ok = False
        print("%-4s _sane_rate(%r) -> %s (期待 %s)" % (mark, v, got, want))

    YENSEN = [(147.0, "147円ちょうど"), (147.238, "147円24銭"),
              (146.999, "147円ちょうど"), (147.05, "147円5銭")]
    for v, want in YENSEN:
        got = server_tools._yen_sen(v)
        mark = "OK" if got == want else "NG"
        if got != want:
            ok = False
        print("%-4s _yen_sen(%s) -> %s (期待 %s)" % (mark, v, got, want))

    # ---- モデルが when を落とした時の補い方（通信なし） ----
    import app  # noqa: E402  ここでしか使わない

    WHEN_TEXT = [
        ("今日はどうなの", "today"),
        ("きょうの天気", "today"),
        ("あしたはどう", "tomorrow"),
        ("明日の大阪", "tomorrow"),
        ("あさっては", "day_after_tomorrow"),
        ("明後日の天気", "day_after_tomorrow"),
        ("今の天気", "now"),
        ("現在の気温", "now"),
        ("じゃあ鳥取はどう", None),
        ("鳥取は", None),
    ]
    for text, want in WHEN_TEXT:
        got = server_tools.when_from_text(text)
        mark = "OK" if got == want else "NG"
        if got != want:
            ok = False
        print("%-4s 発話から when: %-14s -> %s (期待 %s)" % (mark, text, got, want))

    INFER = [
        ({"utterance": "今日はどうなの", "last_when": "tomorrow"}, "today",
         "発話の日が引き継ぎより強い"),
        ({"utterance": "じゃあ鳥取はどう", "last_when": "tomorrow"}, "tomorrow",
         "日を言わない追い質問は直前の日を引き継ぐ"),
        ({"utterance": "鳥取は", "last_when": None}, "today",
         "引き継ぐものが無ければ today"),
        ({"utterance": "鳥取は", "last_when": "ゆるふわ"}, "today",
         "知らない値は引き継がない"),
        (None, "today", "文脈が無くても落ちない"),
    ]
    for ctx, want, memo in INFER:
        got = server_tools.infer_when(ctx)
        mark = "OK" if got == want else "NG"
        if got != want:
            ok = False
        print("%-4s infer_when: %-30s -> %s (期待 %s)" % (mark, memo, got, want))

    STAMPED = [
        ("（2026年7月30日(木) 7時32分）鳥取は", "鳥取は", None),
        ("（2026年7月30日(木) 7時32分）今日はどうなの", "今日はどうなの", "today"),
        ("（7月30日(木)）あしたの大阪", "あしたの大阪", "tomorrow"),
    ]
    for stamped, want_text, want_when in STAMPED:
        hist = [{"role": "user", "content": stamped}]
        got_text = app.last_user_text(hist)
        got_when = server_tools.when_from_text(got_text)
        good = got_text == want_text and got_when == want_when
        if not good:
            ok = False
        print("%-4s 時刻を外して日を読む: %-14s -> %s / %s"
              % ("OK" if good else "NG", stamped[:20], got_text, got_when))

    # 引き継ぎは「少し前に調べた時」だけ（何十分も前の日を継がない）
    import time as _t
    app.when_store.clear()
    checks = [("覚えが無ければ None", app.remembered_when("dev"), None)]
    app.when_store["dev"] = (_t.time(), "tomorrow")
    checks.append(("直後なら引き継ぐ", app.remembered_when("dev"), "tomorrow"))
    app.when_store["dev"] = (_t.time() - app.WHEN_TTL - 1, "tomorrow")
    checks.append(("古ければ引き継がない", app.remembered_when("dev"), None))
    checks.append(("古い覚えは捨てる", "dev" in app.when_store, False))
    app.when_store["other"] = (_t.time(), "now")
    checks.append(("別の機体とは混ざらない", app.remembered_when("dev"), None))
    checks.append(("その機体の分は残る", app.remembered_when("other"), "now"))
    app.when_store.clear()
    for memo, got, want in checks:
        mark = "OK" if got == want else "NG"
        if got != want:
            ok = False
        print("%-4s 引き継ぎ: %-22s -> %s (期待 %s)" % (mark, memo, got, want))

    # 過去の日は今日として答えない
    PASTS = [("昨日の天気", "past"), ("おとといの天気", "past"),
             ("今週の天気は", None), ("今度の日曜は", None),
             ("大阪に住んでいますが天気は", None), ("今の天気", "now"),
             ("昨日は暑かったけど今日はどう", "today")]
    for text, want in PASTS:
        got = server_tools.when_from_text(text)
        mark = "OK" if got == want else "NG"
        if got != want:
            ok = False
        print("%-4s 日の読み取り: %-22s -> %s (期待 %s)" % (mark, text, got, want))

    print("jst:", server_tools.jst_stamp())
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


sys.exit(asyncio.run(main()))
