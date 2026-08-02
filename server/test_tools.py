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


import datetime as _dt
NL = chr(10)


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

        # ---- さくら無料枠カウンタ ----
        import json as _json
        import tempfile as _tempfile
        real_usage = server_tools.USAGE_PATH
        with _tempfile.TemporaryDirectory() as td:
            server_tools.USAGE_PATH = td + "/llm_usage.json"
            server_tools.count_llm_request()
            server_tools.count_llm_request()
            text = await server_tools.call(s, "get_llm_quota", {})
            print("%-28s %s" % ("2回数えて残りを聞く", text))
            if "2回" not in text or "%d回" % (server_tools.SAKURA_QUOTA - 2) not in text:
                ok = False
            # 月が替わっていたら数え直す
            with open(server_tools.USAGE_PATH, "w") as f:
                _json.dump({"month": "1999-01", "count": 500}, f)
            text = await server_tools.call(s, "get_llm_quota", {})
            print("%-28s %s" % ("先月の数字は持ち越さない", text))
            if "0回" not in text.split("無料枠")[0]:
                ok = False
            # 壊れたファイルでも落ちない
            with open(server_tools.USAGE_PATH, "w") as f:
                f.write("not json")
            server_tools.count_llm_request()
            text = await server_tools.call(s, "get_llm_quota", {})
            print("%-28s %s" % ("壊れた記録から回復", text))
            if "1回" not in text:
                ok = False
        server_tools.USAGE_PATH = real_usage

        # ---- 暗号資産 ----
        # 実際に取りに行く（指定なし＝BTC/ETH 両方）
        text = await server_tools.call(s, "get_crypto", {})
        print("%-28s %s" % ("暗号資産を実取得", text))
        if (text.startswith("error:") or "ビットコイン" not in text
                or "イーサリアム" not in text):
            ok = False

        # coin 指定なら 1 つだけ
        text = await server_tools.call(s, "get_crypto", {"coin": "btc"})
        print("%-28s %s" % ("ビットコインだけ", text))
        if text.startswith("error:") or "イーサリアム" in text:
            ok = False

        # 取得先が全部死んでいる: 新しいキャッシュがあればそのまま読む
        real_gecko = server_tools.CRYPTO_URL_GECKO
        real_hosts2 = server_tools.STOCK_HOSTS
        server_tools.CRYPTO_URL_GECKO = "http://127.0.0.1:9/price"
        server_tools.STOCK_HOSTS = ("127.0.0.1:9",)
        text = await server_tools.call(s, "get_crypto", {"coin": "btc"})
        print("%-28s %s" % ("死んでいるが直前の値あり", text))
        if text.startswith("error:") or "分前の情報" in text:
            ok = False

        # 古い（15分超）キャッシュへ退避したら古さを言う
        _ts, _line = server_tools._crypto_cache["btc"]
        server_tools._crypto_cache["btc"] = (
            server_tools.time.monotonic() - 1800, _line)
        text = await server_tools.call(s, "get_crypto", {"coin": "btc"})
        print("%-28s %s" % ("死んでいるが古い値あり", text))
        if "分前の情報" not in text:
            ok = False

        # 古すぎる値は使わない
        server_tools._crypto_cache["btc"] = (
            server_tools.time.monotonic() - server_tools.CRYPTO_STALE_MAX - 60, _line)
        text = await server_tools.call(s, "get_crypto", {"coin": "btc"})
        print("%-28s %s" % ("古すぎる値は使わない", text))
        if "調べられませんでした" not in text:
            ok = False

        server_tools._crypto_cache.clear()
        server_tools.CRYPTO_URL_GECKO = real_gecko
        server_tools.STOCK_HOSTS = real_hosts2

        # ---- 台風・熱中症 ----
        text = await server_tools.call(s, "get_typhoon", {})
        print("%-28s %s" % ("台風情報を実取得", text[:80]))
        if text.startswith("error:") or "台風" not in text:
            ok = False

        real = server_tools.TYPHOON_LIST_URL
        server_tools.TYPHOON_LIST_URL = "http://127.0.0.1:9/tc"
        text = await server_tools.call(s, "get_typhoon", {})
        print("%-28s %s" % ("台風: 直前の値で答える", text[:50]))
        if text.startswith("error:"):
            ok = False
        server_tools._typhoon_cache[:] = [(
            server_tools.time.monotonic() - server_tools.TYPHOON_STALE_MAX - 60,
            "古い台風")]
        text = await server_tools.call(s, "get_typhoon", {})
        print("%-28s %s" % ("台風: 古すぎる値は使わない", text))
        if "取れませんでした" not in text:
            ok = False
        server_tools._typhoon_cache.clear()
        server_tools.TYPHOON_LIST_URL = real

        SPEC = [{"part": "title", "typhoonNumber": "2613",
                 "name": {"jp": "ドルフィン"}},
                {"part": {"jp": "実況"}, "location": "南鳥島近海",
                 "intensity": "非常に強い", "course": "北西",
                 "speed": {"km/h": "20"}, "pressure": "935"}]
        line = server_tools._typhoon_one(SPEC)
        good = ("台風13号ドルフィン" in line and "南鳥島近海" in line
                and "時速20キロ" in line and "935ヘクトパスカル" in line)
        ok = ok and good
        print("%-4s _typhoon_one: %s" % ("OK" if good else "NG", line[:60]))

        text = await server_tools.call(s, "get_heat", {})
        print("%-28s %s" % ("暑さ指数を実取得", text[:80]))
        if text.startswith("error:") or "暑さ指数" not in text:
            ok = False

        WB = [(33.0, "危険"), (29.0, "厳重警戒"), (26.0, "警戒"),
              (22.0, "注意"), (18.0, "ほぼ安全")]
        for v, want in WB:
            got = server_tools._wbgt_level(v)
            mark = "OK" if got == want else "NG"
            if got != want:
                ok = False
            print("%-4s _wbgt_level(%s) -> %s (期待 %s)" % (mark, v, got, want))
        CSV = (",," + ",".join(["2026080115", "2026080118", "2026080203"]) + NL
               + "46106,2026/08/01 15:25, 290, 310, 240")
        cur, peak = server_tools._heat_parse_fcst(
            CSV, _dt.datetime(2026, 8, 1, 16, 0, tzinfo=server_tools.JST))
        good = abs(cur - 29.0) < 0.01 and abs(peak - 31.0) < 0.01
        ok = ok and good
        print("%-4s _heat_parse_fcst -> いま %.1f / 最高 %.1f"
              % ("OK" if good else "NG", cur, peak))
        AL = ("Title,熱中症警戒情報" + NL
              + "神奈川県,46,0,140000,神奈川,14,1,0,他" + NL
              + "東京都,44,0,130000,東京,13,0,0,他")
        t1, t2 = server_tools._heat_parse_alert(AL, "神奈川県")
        good = (t1, t2) == ("1", "0")
        ok = ok and good
        print("%-4s _heat_parse_alert -> きょう %s / あす %s"
              % ("OK" if good else "NG", t1, t2))

        # ---- 今日は何の日・月と日の出入り・電車 ----
        text = await server_tools.call(s, "get_onthisday", {})
        print("%-28s %s" % ("今日は何の日を実取得", text[:80]))
        if text.startswith("error:") or "出来事" not in text:
            ok = False

        WIKI = ("== [[8月1日]] ==" + NL
                + "* [[ジョゼフ・プリーストリー]]が[[酸素]]を発見（[[1774年]]）" + NL
                + "* [[ベナン]]独立（[[1960年]]）" + NL + NL
                + "== [[8月2日]] ==" + NL + "* 別の日の話")
        ev = server_tools._onthisday_parse(WIKI, 8, 1)
        good = (len(ev) == 2 and ev[0] == "1774年にジョゼフ・プリーストリーが酸素を発見"
                and ev[1] == "1960年にベナン独立")
        ok = ok and good
        print("%-4s _onthisday_parse -> %s" % ("OK" if good else "NG", ev))
        good = (server_tools._wiki_plain("[[8月1日|きょう]]は''強調''{{注}}")
                == "きょうは強調"
                and server_tools._wiki_plain("[[a|b]]は{{x|{{y}}}}だ<ref>注</ref>")
                == "bはだ注")
        ok = ok and good
        print("%-4s _wiki_plain: リンクと装飾を落とす" % ("OK" if good else "NG"))

        text = await server_tools.call(s, "get_sky", {})
        print("%-28s %s" % ("月齢と日の出入り", text[:80]))
        if "月齢" not in text or "日の出" not in text:
            ok = False
        SUN = [("2026-06-21", 4, 27, 19, 0), ("2026-12-22", 6, 48, 16, 33)]
        for ds, rh, rm, sh, sm in SUN:
            d = _dt.datetime.fromisoformat(ds + "T12:00:00+09:00")
            r, st = server_tools._sun_events(d)
            good = (abs((r.hour * 60 + r.minute) - (rh * 60 + rm)) <= 3
                    and abs((st.hour * 60 + st.minute) - (sh * 60 + sm)) <= 3)
            ok = ok and good
            print("%-4s _sun_events(%s) -> %02d:%02d / %02d:%02d (公表 %02d:%02d / %02d:%02d)"
                  % ("OK" if good else "NG", ds, r.hour, r.minute,
                     st.hour, st.minute, rh, rm, sh, sm))
        # 国立天文台の朔弦望（2026-07-14 18:44 新月 / 2026-07-29 23:36 満月）で照合
        AGE = [(_dt.datetime(2026, 7, 14, 18, 44, tzinfo=server_tools.JST),
                0.0, "新月"),
               (_dt.datetime(2026, 7, 29, 23, 36, tzinfo=server_tools.JST),
                server_tools.SYNODIC / 2, "満月"),
               (_dt.datetime(2026, 8, 13, 2, 37, tzinfo=server_tools.JST),
                0.0, "次の新月"),
               (_dt.datetime(2026, 8, 28, 13, 19, tzinfo=server_tools.JST),
                server_tools.SYNODIC / 2, "次の満月")]
        for d, want, memo in AGE:
            got = server_tools._moon_age(d)
            diff = min(abs(got - want), abs(got - want - server_tools.SYNODIC),
                       abs(got - want + server_tools.SYNODIC))
            good = diff <= 0.35
            ok = ok and good
            print("%-4s _moon_age(%s %s) -> %.2f (公表の%s・ずれ %.2f日)"
                  % ("OK" if good else "NG", d.date(), d.strftime("%H:%M"),
                     got, memo, diff))

        # 査読で出た穴（夏期以外・アラートの版・トークン秘匿・鍵切れ退避）
        real_season = server_tools.HEAT_SEASON_FROM
        _sv = server_tools._heat_in_season
        good = (_sv(_dt.datetime(2026, 8, 1, tzinfo=server_tools.JST))
                and not _sv(_dt.datetime(2026, 1, 15, tzinfo=server_tools.JST))
                and not _sv(_dt.datetime(2026, 10, 25, tzinfo=server_tools.JST)))
        ok = ok and good
        print("%-4s 暑さ指数は夏期だけ提供と分かっている" % ("OK" if good else "NG"))
        server_tools.HEAT_SEASON_FROM = "12-31"
        text = await server_tools.call(s, "get_heat", {})
        good = "取れませんでした" not in text and "出ていません" in text
        ok = ok and good
        print("%-4s 期間外は落ちてると言わない: %s" % ("OK" if good else "NG", text[:40]))
        server_tools.HEAT_SEASON_FROM = real_season

        _d = _dt.datetime(2026, 8, 1, 18, 0, tzinfo=server_tools.JST)
        vers = server_tools._heat_alert_versions(_d)
        good = vers[0][1] == "17" and vers[0][0].day == 1
        ok = ok and good
        print("%-4s 夕方は17時版を先に見る" % ("OK" if good else "NG"))
        vers = server_tools._heat_alert_versions(_d.replace(hour=2))
        good = vers[0][1] == "17" and vers[0][0].day == 31
        ok = ok and good
        print("%-4s 未明は前日17時版に落ちる" % ("OK" if good else "NG"))

        real_tok = server_tools.ODPT_TOKEN
        server_tools.ODPT_TOKEN = "SECRET123"
        masked = server_tools._hide_token("url=https://x?acl:consumerKey=SECRET123")
        good = "SECRET123" not in masked and "***" in masked
        ok = ok and good
        print("%-4s トークンはログに出さない: %s" % ("OK" if good else "NG", masked))
        server_tools.ODPT_KEYED_URL = "http://127.0.0.1:9/keyed"
        server_tools._train_cache.clear()
        text = await server_tools.call(s, "get_train", {})
        good = not text.startswith("error:") and "都営" in text
        ok = ok and good
        print("%-4s 鍵付きが死んでも公開分で答える: %s" % ("OK" if good else "NG", text[:40]))
        server_tools.ODPT_TOKEN = real_tok
        server_tools._train_cache.clear()

        long_ev = ["あ" * 60, "い" * 60, "う" * 60]
        server_tools._onthisday_cache[
            "%02d-%02d" % (server_tools.now_jst().month, server_tools.now_jst().day)
        ] = (server_tools.time.monotonic(), long_ev)
        text = await server_tools.call(s, "get_onthisday", {})
        good = len(text) <= server_tools.ONTHISDAY_MAX_CHARS + 10
        ok = ok and good
        print("%-4s 長い日は件数を減らす -> %d字" % ("OK" if good else "NG", len(text)))
        server_tools._onthisday_cache.clear()

        text = await server_tools.call(s, "get_train", {})
        print("%-28s %s" % ("運行情報を実取得", text[:80]))
        if text.startswith("error:") or "遅れ" not in text and "いま、" not in text:
            ok = False
        TR = [{"odpt:railway": "odpt.Railway:Keikyu.Main",
               "odpt:trainInformationStatus": {"ja": "遅延"},
               "odpt:trainInformationCause": {"ja": "人身事故"}},
              {"odpt:railway": "odpt.Railway:JR-East.Yokohama",
               "odpt:trainInformationStatus": {"ja": "運転見合わせ"}},
              {"odpt:railway": "odpt.Railway:Toei.Mita"}]
        line = server_tools._train_line(TR, True)
        good = ("京急本線は遅延" in line and "JRの横浜線は運転見合わせ" in line
                and "人身事故" in line)
        ok = ok and good
        print("%-4s _train_line(異常あり): %s" % ("OK" if good else "NG", line))
        line = server_tools._train_line([TR[2]], False)
        good = "大きな遅れは出ていません" in line and "都営地下鉄しか" in line
        ok = ok and good
        print("%-4s _train_line(平常/キー無し): %s" % ("OK" if good else "NG", line))

        TR2 = [{"odpt:railway": "odpt.Railway:TokyoMetro.Ginza",
                "odpt:trainInformationStatus": {"ja": "遅延"}},
               {"odpt:railway": "odpt.Railway:YokohamaMunicipal.Blue",
                "odpt:trainInformationStatus": {"ja": "運転見合わせ"}},
               {"odpt:railway": "odpt.Railway:Keikyu.Main",
                "odpt:trainInformationStatus": {"ja": "遅延"}}]
        line = server_tools._train_line(TR2, True)
        good = (line.index("京急本線") < line.index("横浜市営地下鉄ブルーライン")
                < line.index("地下鉄銀座線"))
        ok = ok and good
        print("%-4s _train_line(京急を先に読む): %s" % ("OK" if good else "NG", line))

        # ---- ニュース・地震・警報 ----
        text = await server_tools.call(s, "get_news", {})
        print("%-28s %s" % ("ニュースを実取得", text[:80]))
        if text.startswith("error:") or "主なニュース" not in text:
            ok = False

        # 取得先が死んでいる: 新しいキャッシュがあればそのまま読む
        real_news = server_tools.NEWS_URL
        server_tools.NEWS_URL = "http://127.0.0.1:9/rss"
        text = await server_tools.call(s, "get_news", {})
        print("%-28s %s" % ("死んでいるが直前の値あり", text[:60]))
        if text.startswith("error:"):
            ok = False
        # 古すぎる値は使わない
        server_tools._news_cache[:] = [(
            server_tools.time.monotonic() - server_tools.NEWS_STALE_MAX - 60,
            ["古い見出し"])]
        text = await server_tools.call(s, "get_news", {})
        print("%-28s %s" % ("古すぎる値は使わない", text))
        if "取れませんでした" not in text:
            ok = False
        server_tools._news_cache.clear()
        server_tools.NEWS_URL = real_news

        text = await server_tools.call(s, "get_quake", {})
        print("%-28s %s" % ("地震情報を実取得", text))
        if text.startswith("error:") or "震" not in text:
            ok = False

        text = await server_tools.call(s, "get_warning", {})
        print("%-28s %s" % ("警報・注意報を実取得", text))
        if text.startswith("error:") or "神奈川県" not in text:
            ok = False

        # 通信なしの単体（時刻の読み・震度表記・警報の有効判定）
        _now = _dt.datetime(2026, 8, 1, 15, 0, tzinfo=server_tools.JST)
        FQT = [("2026-08-01T13:00:00+09:00", "きょう13時0分ごろ"),
               ("2026-07-31T23:59:00+09:00", "きのう23時59分ごろ"),
               ("2026-07-20T01:05:00+09:00", "7月20日1時5分ごろ")]
        for iso, want in FQT:
            got = server_tools._fmt_quake_time(iso, _now)
            mark = "OK" if got == want else "NG"
            if got != want:
                ok = False
            print("%-4s _fmt_quake_time(%s) -> %s (期待 %s)" % (mark, iso, got, want))
        QU = [{"at": "2026-08-01T13:00:00+09:00", "anm": "熊本県熊本地方",
               "mag": "3.0", "maxi": "5-", "eid": "a"},
              {"at": "2026-08-01T05:00:00+09:00", "anm": "どこか",
               "mag": "2.5", "maxi": "1", "eid": "b"}]
        line = server_tools._quake_line(QU, _now)
        good = "5弱" in line and "マグニチュード3.0" in line and "2回" in line
        ok = ok and good
        print("%-4s _quake_line: %s" % ("OK" if good else "NG", line))
        WB = {"areaTypes": [{"areas": [{"warnings": [
            {"code": "03", "status": "発表"}, {"code": "15", "status": "継続"},
            {"code": "04", "status": "解除"}]}]}]}
        names = server_tools._active_warnings(WB)
        good = names == ["大雨警報", "強風注意報"]
        ok = ok and good
        print("%-4s _active_warnings: %s" % ("OK" if good else "NG", names))


        # 読み上げ書式と門番（通信なし）
        JPY = [(9911652, "991万円"), (294099, "29万円"), (123456789, "1.2億円"),
               (9800, "9800円")]
        for v, want in JPY:
            got = server_tools._fmt_jpy_about(v)
            mark = "OK" if got == want else "NG"
            if got != want:
                ok = False
            print("%-4s _fmt_jpy_about(%s) -> %s (期待 %s)" % (mark, v, got, want))
        try:
            server_tools._crypto_line("btc", 35600000 * 1000, None)
            print("NG   桁違いの値を読んでしまう")
            ok = False
        except RuntimeError:
            print("OK   桁違いの値は読まない")



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
