"""サーバー側で実行するツール群。

本体（ESP32）が MCP で見せてくるツールは機体の操作（音量など）しかない。
会話として役に立つには「今日の天気」のように外の情報が要るので、そこはサーバーが持つ。
LLM には本体のツールとこちらのツールを 1 つの配列にまとめて渡し、
呼ばれた名前で振り分ける（app.py の call_tool）。

天気の取得先はサイト（yokohama-funnies / minami-baseball-ob の lib/weather.ts）と
同じ Open-Meteo に揃えた。API キー不要・無料。
"""
import asyncio
import datetime
import logging
import os
import time
import urllib.parse

import aiohttp

from places import PLACES

log = logging.getLogger("stackchan.tools")

JST = datetime.timezone(datetime.timedelta(hours=9), "JST")
WDAY = ["月", "火", "水", "木", "金", "土", "日"]

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
DEFAULT_PLACE = os.environ.get("WEATHER_PLACE", "横浜")
CACHE_TTL = float(os.environ.get("WEATHER_CACHE_TTL", "600"))

# WMO 天気コード -> 日本語。サイトの lib/weather.ts と同じ表
WEATHER_LABELS = {
    0: "快晴", 1: "晴れ", 2: "晴れ時々くもり", 3: "くもり",
    45: "霧", 48: "凍る霧",
    51: "小雨", 53: "霧雨", 55: "強い雨", 56: "凍雨（弱）", 57: "凍雨（強）",
    61: "小雨", 63: "雨", 65: "大雨", 66: "凍雨（弱）", 67: "凍雨（強）",
    71: "小雪", 73: "雪", 75: "大雪", 77: "霧雪",
    80: "にわか雨（弱）", 81: "にわか雨", 82: "激しいにわか雨",
    85: "にわか雪（弱）", 86: "にわか雪（強）",
    95: "雷雨", 96: "雷雨（ひょう）", 99: "激しい雷雨（ひょう）",
}
SUFFIX = ("都", "道", "府", "県", "市", "区", "町", "村")

_forecast_cache = {}   # (lat, lon) -> (取得時刻, 応答)
_geocode_cache = {}    # 地名 -> (lat, lon, 表示名) / None


def now_jst() -> datetime.datetime:
    return datetime.datetime.now(JST)


def jst_stamp() -> str:
    t = now_jst()
    return "%d年%d月%d日(%s) %d時%02d分" % (
        t.year, t.month, t.day, WDAY[t.weekday()], t.hour, t.minute)


def label(code) -> str:
    try:
        return WEATHER_LABELS.get(int(code), "不明")
    except (TypeError, ValueError):
        return "不明"


def _strip_suffix(name: str) -> str:
    w = name.strip()
    while len(w) > 2 and w[-1] in SUFFIX:
        w = w[:-1]
    return w


async def resolve_place(session, name: str):
    """地名 -> (緯度, 経度, 表示名)。見つからなければ None。

    日本語名は同梱の表（gen_places.py が geocoding から生成）で引く。
    Open-Meteo の geocoding は日本語名を引けない（「札幌」は 0 件）ので、
    表に無い場合の追加検索は ASCII の名前に限る。
    """
    key = (name or "").strip()
    if not key:
        return None
    for cand in (key, key.lower(), _strip_suffix(key)):
        hit = PLACES.get(cand)
        if hit:
            return hit
    if not key.isascii():
        return None
    if key.lower() in _geocode_cache:
        return _geocode_cache[key.lower()]
    url = GEOCODE_URL + "?" + urllib.parse.urlencode(
        {"name": key, "count": 20, "language": "ja", "format": "json"})
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
            body = await r.json()
    except Exception:
        log.exception("geocode failed: %s", key)
        return None
    found = None
    for h in body.get("results") or []:
        if h.get("country_code") == "JP":
            found = (round(h["latitude"], 4), round(h["longitude"], 4),
                     h.get("name") or key)
            break
    _geocode_cache[key.lower()] = found
    return found


async def _forecast(session, lat: float, lon: float):
    """Open-Meteo の予報。取れなければ (古い値, 経過秒) で返す。

    無料の公開 API なので 503（過負荷）が普通に返ってくる。読み上げる相手に
    生のエラーを聞かせないよう、短く粘ってから最後の成功値へ退避する。
    """
    ck = (lat, lon)
    hit = _forecast_cache.get(ck)
    if hit and time.monotonic() - hit[0] < CACHE_TTL:
        return hit[1], 0.0
    url = FORECAST_URL + "?" + urllib.parse.urlencode({
        "latitude": lat, "longitude": lon, "timezone": "Asia/Tokyo",
        "wind_speed_unit": "ms",
        "current": "temperature_2m,weather_code,wind_speed_10m,relative_humidity_2m",
        "daily": ("weather_code,temperature_2m_max,temperature_2m_min,"
                  "precipitation_probability_max"),
        "forecast_days": 3,
    })
    last = ""
    for attempt in range(3):
        if attempt:
            await asyncio.sleep(0.6 * attempt)
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                body = await r.json()
                if r.status == 200:
                    _forecast_cache[ck] = (time.monotonic(), body)
                    return body, 0.0
                last = "http %d %s" % (r.status, str(body)[:120])
        except Exception as e:
            last = "%s: %s" % (type(e).__name__, e)
        log.warning("open-meteo retry %d (%s)", attempt + 1, last)
    if hit:
        age = time.monotonic() - hit[0]
        log.warning("open-meteo failed, falling back to cache (%.0fs old)", age)
        return hit[1], age
    raise RuntimeError(last or "open-meteo unreachable")


WHEN_OFFSET = {"today": 0, "tomorrow": 1, "day_after_tomorrow": 2}


async def get_weather(session, args) -> str:
    place = str(args.get("place") or DEFAULT_PLACE)
    when = str(args.get("when") or "today")
    spot = await resolve_place(session, place)
    if spot is None:
        return "error: 「%s」の場所が分かりませんでした" % place
    lat, lon, shown = spot
    try:
        data, age = await _forecast(session, lat, lon)
    except Exception as e:
        log.warning("weather unavailable for %s: %s", shown, e)
        return "error: いま天気を調べられませんでした（取得先が応答しません）"
    stale = " ※%d分前の情報" % int(age / 60) if age > 900 else ""
    cur = data.get("current") or {}
    daily = data.get("daily") or {}
    now_part = "現在 %.1f度 %s 湿度%d%% 風%.1fm/s" % (
        cur.get("temperature_2m", 0.0), label(cur.get("weather_code")),
        int(cur.get("relative_humidity_2m") or 0), cur.get("wind_speed_10m", 0.0))
    if when == "now":
        return "%s の天気: %s%s" % (shown, now_part, stale)
    i = WHEN_OFFSET.get(when, 0)
    days = daily.get("time") or []
    if i >= len(days):
        return "error: %s の予報は取れません" % when
    d = datetime.date.fromisoformat(days[i])
    head = "%d月%d日(%s)" % (d.month, d.day, WDAY[d.weekday()])
    line = "%s %s %s 最高%.1f度 最低%.1f度 降水確率%d%%" % (
        shown, head, label((daily.get("weather_code") or [None])[i]),
        (daily.get("temperature_2m_max") or [0])[i],
        (daily.get("temperature_2m_min") or [0])[i],
        int((daily.get("precipitation_probability_max") or [0])[i] or 0))
    if i == 0:
        line = line + " / " + now_part
    return line + stale


SPECS = [{
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "指定した場所の天気を調べる。今の天気・今日/明日/明後日の予報。"
                       "場所を言われなければ既定（" + DEFAULT_PLACE + "）で調べる。",
        "parameters": {
            "type": "object",
            "properties": {
                "place": {"type": "string",
                          "description": "地名。例: 横浜、東京、大阪。省略可"},
                "when": {"type": "string",
                         "enum": ["now", "today", "tomorrow", "day_after_tomorrow"],
                         "description": "いつの天気か。既定は today"},
            },
        },
    },
}]

HANDLERS = {"get_weather": get_weather}


def specs():
    return SPECS


def has(name: str) -> bool:
    return name in HANDLERS


async def call(session, name: str, args: dict) -> str:
    return await HANDLERS[name](session, args or {})
