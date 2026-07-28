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
    ({"place": "ぬるぽ市"}, "知らない地名は素直に分からないと言う"),
]


async def main():
    ok = True
    async with aiohttp.ClientSession() as s:
        for args, memo in CASES:
            text = await server_tools.call(s, "get_weather", args)
            print("%-28s %s" % (memo, text))
            bad = text.startswith("error:")
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
        server_tools.FORECAST_URL = real

    print("jst:", server_tools.jst_stamp())
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


sys.exit(asyncio.run(main()))
