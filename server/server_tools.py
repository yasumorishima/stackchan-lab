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
import json
import logging
import math
import os
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET

import aiohttp

from places import PLACES

log = logging.getLogger("stackchan.tools")

JST = datetime.timezone(datetime.timedelta(hours=9), "JST")
WDAY = ["月", "火", "水", "木", "金", "土", "日"]

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
DEFAULT_PLACE = os.environ.get("WEATHER_PLACE", "横浜")
CACHE_TTL = float(os.environ.get("WEATHER_CACHE_TTL", "600"))
# 取得先が落ちている時に古い値で答える上限 [s]。これを超えたら黙って古い値を読まない
STALE_MAX = float(os.environ.get("WEATHER_STALE_MAX", "21600"))
MAX_PLACE_LEN = 40      # LLM が返す引数は信用しない
CACHE_MAX = 64

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


def _part(fmt, v):
    """値があるときだけ書式化する。

    API は値に null を返すことがある。そこを 0 で埋めると「最高 0.0 度」と
    読み上げてしまうので、欠けた項目は言わない。
    """
    try:
        return fmt % float(v)
    except (TypeError, ValueError):
        return None


def _join(parts, sep=" "):
    return sep.join(p for p in parts if p)


def _trim(cache, limit=CACHE_MAX):
    while len(cache) > limit:
        cache.pop(next(iter(cache)), None)


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
    key = (name or "").strip()[:MAX_PLACE_LEN]
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
    # 先頭が目的地とは限らない（gen_places.py と同じ選び方をする）
    jp = [h for h in (body.get("results") or []) if h.get("country_code") == "JP"]
    rank = {"PPLC": 3, "PPLA": 2, "PPLA2": 1}
    jp.sort(key=lambda h: (rank.get(h.get("feature_code"), 0), h.get("population") or 0),
            reverse=True)
    found = None
    if jp:
        found = (round(jp[0]["latitude"], 4), round(jp[0]["longitude"], 4),
                 jp[0].get("name") or key)
    _geocode_cache[key.lower()] = found
    _trim(_geocode_cache, 256)
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
                    _trim(_forecast_cache)
                    return body, 0.0
                last = "http %d %s" % (r.status, str(body)[:120])
        except Exception as e:
            last = "%s: %s" % (type(e).__name__, e)
        log.warning("open-meteo retry %d (%s)", attempt + 1, last)
    if hit:
        age = time.monotonic() - hit[0]
        if age > STALE_MAX:
            log.warning("cached forecast too old (%.0fs), not using it", age)
            raise RuntimeError("no fresh forecast (cache %.0fs old)" % age)
        log.warning("open-meteo failed, falling back to cache (%.0fs old)", age)
        return hit[1], age
    raise RuntimeError(last or "open-meteo unreachable")


WHEN_OFFSET = {"today": 0, "tomorrow": 1, "day_after_tomorrow": 2}
WHEN_VALUES = ("now", "today", "tomorrow", "day_after_tomorrow")

# 発話から日を読む表。長い語を先に見る（「明後日」を「明日」で拾わないため、
# また「今日」を「今」で拾わないため、並び順に意味がある）
PAST = "past"
DAY_WORDS = (
    ("day_after_tomorrow", ("明後日", "あさって", "アサッテ")),
    ("tomorrow", ("明日", "あした", "あす", "アシタ")),
    ("today", ("今日", "きょう", "本日", "キョウ")),
    ("now", ("現在", "今の", "いまの", "今は", "いまは", "今すぐ",
             "只今", "ただいま", "現時点")),
    # 過去は最後に見る（「昨日は暑かったけど今日はどう」は today を採りたい）
    (PAST, ("一昨日", "おととい", "昨日", "きのう", "昨夜", "ゆうべ")),
)


def when_from_text(text: str):
    """発話に日を表す語があれば返す。無ければ None。"""
    text = text or ""
    for when, words in DAY_WORDS:
        for w in words:
            if w in text:
                return when
    return None


def infer_when(ctx) -> str:
    """モデルが when を落としたときの補い方。推測せず順に確かめる。

    1. 発話に日を表す語があればそれを使う（「今日はどうなの」→ today）
    2. 無ければ同じ会話で直前に調べた日を引き継ぐ（「じゃあ鳥取は？」）
    3. どちらも無ければ today

    2 が要るのは、省略形の追い質問で qwen2.5:3b が when を落とすため。
    既定の today で埋めると「あしたの大阪」の直後の「じゃあ鳥取は？」に
    今日の天気を答えてしまい、しかも本人は明日だと思って聞いている。
    when を required にして解決しようとすると、モデルがツールを呼ぶのを
    やめて数値を作り話した（2026-07-30 実測）ので、サーバー側で補う。
    """
    ctx = ctx or {}
    w = when_from_text(ctx.get("utterance") or "")
    if w:
        log.info("when を発話から補った: %s", w)
        return w
    prev = ctx.get("last_when")
    if prev in WHEN_VALUES:
        log.info("when を直前の質問から引き継いだ: %s", prev)
        return prev
    return "today"


async def get_weather(session, args, ctx=None) -> str:
    place = str(args.get("place") or DEFAULT_PLACE)
    when = str(args.get("when") or "")
    if when not in WHEN_VALUES:
        if when:
            log.warning("知らない when=%r は無視して補う", when[:20])
        when = infer_when(ctx)
    if when == PAST:
        # 予報しか取れないので、今日として答えず分からないと言う
        return "error: 過去の天気は分かりません。今日からあさってまでなら調べられます"
    if isinstance(ctx, dict):
        # 次の追い質問で引き継げるように、実際に使った値を呼び出し側へ返す
        ctx["resolved_when"] = when
    spot = await resolve_place(session, place)
    if spot is None:
        return "error: 「%s」の場所が分かりませんでした" % place[:20]
    lat, lon, shown = spot
    try:
        data, age = await _forecast(session, lat, lon)
    except Exception as e:
        log.warning("weather unavailable for %s: %s", shown, e)
        return "error: いま天気を調べられませんでした（取得先が応答しません）"
    stale = " ※%d分前の情報" % int(age / 60) if age > 900 else ""
    cur = data.get("current") or {}
    daily = data.get("daily") or {}
    now_bits = [_part("%.1f度", cur.get("temperature_2m")),
                label(cur.get("weather_code")) if cur.get("weather_code") is not None else None,
                _part("湿度%.0f%%", cur.get("relative_humidity_2m")),
                _part("風%.1fm/s", cur.get("wind_speed_10m"))]
    now_part = ("現在 " + _join(now_bits)) if any(now_bits) else ""
    if when == "now":
        if not now_part:
            return "error: %s の実況が取れませんでした" % shown
        return "%s の天気: %s%s" % (shown, now_part, stale)
    i = WHEN_OFFSET.get(when, 0)
    days = daily.get("time") or []
    if i >= len(days):
        return "error: %s の予報は取れません" % when
    d = datetime.date.fromisoformat(days[i])
    head = "%d月%d日(%s)" % (d.month, d.day, WDAY[d.weekday()])
    code = (daily.get("weather_code") or [None])[i]
    bits = [label(code) if code is not None else None,
            _part("最高%.1f度", (daily.get("temperature_2m_max") or [None])[i]),
            _part("最低%.1f度", (daily.get("temperature_2m_min") or [None])[i]),
            _part("降水確率%.0f%%",
                  (daily.get("precipitation_probability_max") or [None])[i])]
    if not any(bits):
        # 日付と地名だけ読み上げても意味がない。取れなかったと言う
        return "error: %s の予報が取れませんでした" % shown
    line = _join([shown, head] + bits)
    if i == 0 and now_part:
        line = line + " / " + now_part
    return line + stale


# ---- ドル円レート ----------------------------------------------------------
# 主は Yahoo Finance の相場データ（キー不要・市場の実勢。週末は金曜終値で止まる）。
# 予備1 は Coinbase の公開レート。暗号資産の板から導いた値なので、市場が閉じて
# いる週末などに実勢から % 単位でずれることがある（2026-08-01 の三点照合では
# 一致していたが、構造的にずれ得るので主にしない）。予備2 は open.er-api.com
# （1日1回更新）。すべて無料・キー不要。
RATE_URL_YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/USDJPY=X"
RATE_URL_COINBASE = "https://api.coinbase.com/v2/exchange-rates"
RATE_URL_DAILY = "https://open.er-api.com/v6/latest/USD"
RATE_CACHE_TTL = float(os.environ.get("RATE_CACHE_TTL", "300"))
RATE_STALE_MAX = float(os.environ.get("RATE_STALE_MAX", "21600"))

_rate_cache = []   # [(取得時刻 monotonic, レート, 取得元)] を 1 件だけ


def _sane_rate(v):
    """レートとして信じられる値か。取得先の障害で 0 や桁違いが来ても読まない。"""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if 50.0 <= v <= 500.0 else None


async def _rate_yahoo(session):
    # UA 無しだと 429 を返すことがある
    async with session.get(RATE_URL_YAHOO, params={"interval": "1d", "range": "1d"},
                           headers={"User-Agent": "Mozilla/5.0"},
                           timeout=aiohttp.ClientTimeout(total=6)) as r:
        body = await r.json()
    meta = (((body.get("chart") or {}).get("result") or [{}])[0] or {}).get("meta") or {}
    return _sane_rate(meta.get("regularMarketPrice")), "リアルタイム"


async def _rate_coinbase(session):
    async with session.get(RATE_URL_COINBASE, params={"currency": "USD"},
                           timeout=aiohttp.ClientTimeout(total=6)) as r:
        body = await r.json()
    return _sane_rate(((body.get("data") or {}).get("rates") or {}).get("JPY")), "リアルタイム"


async def _rate_daily(session):
    async with session.get(RATE_URL_DAILY,
                           timeout=aiohttp.ClientTimeout(total=10)) as r:
        body = await r.json()
    return _sane_rate((body.get("rates") or {}).get("JPY")), "1日1回更新の参考値"


async def _fetch_usdjpy(session):
    """USD/JPY を主→予備の順で取る。返り値は (レート, 取得元の説明)。"""
    last = ""
    for name, fetch in (("yahoo", _rate_yahoo), ("coinbase", _rate_coinbase),
                        ("daily", _rate_daily)):
        try:
            rate, src = await fetch(session)
            if rate is not None:
                return rate, src
            last = "%s gave unusable value" % name
        except Exception as e:
            last = "%s: %s: %s" % (name, type(e).__name__, e)
        log.warning("usdjpy %s", last)
    raise RuntimeError(last or "usdjpy unreachable")


def _yen_sen(rate: float) -> str:
    """147.238 -> 「147円24銭」。小数で返すと読み上げが不自然になる。"""
    yen = int(rate)
    sen = int(round((rate - yen) * 100))
    if sen >= 100:
        yen, sen = yen + 1, sen - 100
    return "%d円%d銭" % (yen, sen) if sen else "%d円ちょうど" % yen


async def get_usdjpy(session, args, ctx=None) -> str:
    now = time.monotonic()
    if _rate_cache and now - _rate_cache[0][0] < RATE_CACHE_TTL:
        ts, rate, src = _rate_cache[0]
    else:
        try:
            rate, src = await _fetch_usdjpy(session)
            ts = now
            _rate_cache[:] = [(ts, rate, src)]
        except Exception as e:
            log.warning("usdjpy unavailable: %s: %s", type(e).__name__, e)
            if _rate_cache and now - _rate_cache[0][0] < RATE_STALE_MAX:
                ts, rate, src = _rate_cache[0]
            else:
                return "error: いまドル円レートを調べられませんでした（取得先が応答しません）"
    age = now - ts
    stale = " ※%d分前の情報" % int(age / 60) if age > 900 else ""
    return "ドル円レート: 1ドル %s（%s）%s" % (_yen_sen(rate), src, stale)


# ---- 株価指数 --------------------------------------------------------------
# ドル円と同じ Yahoo Finance の chart API（キー不要・無料）。指数を返す無料の
# 予備 API が見つからなかったので、多重化はホスト冗長（query1 -> query2）と
# 6 時間の stale cache で担う。市場が閉まっている間は regularMarketPrice が
# 直近の終値のまま止まるので、取引時間外なら「終値」と断って読み上げる。
STOCK_INDEXES = {
    "nikkei": ("^N225", "日経平均株価", "円"),
    "dow": ("^DJI", "ダウ平均株価", "ドル"),
    "sp500": ("^GSPC", "エスアンドピー500", "ポイント"),
    "nasdaq": ("^IXIC", "ナスダック総合指数", "ポイント"),
}
STOCK_HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")
STOCK_CACHE_TTL = float(os.environ.get("STOCK_CACHE_TTL", "300"))
STOCK_STALE_MAX = float(os.environ.get("STOCK_STALE_MAX", "21600"))

_stock_cache = {}   # index キー -> (取得時刻 monotonic, 読み上げ文)


def _fmt_points(value, unit):
    """64362.02, "円" -> 「64362円」。読み上げ用に整数へ丸める（銭・小数は言わない）。"""
    return "%d%s" % (int(round(float(value))), unit)


async def _stock_yahoo(session, symbol):
    """chart API の meta を返す。query1 が死んでいたら query2 を試す。"""
    err = ""
    for host in STOCK_HOSTS:
        url = "https://%s/v8/finance/chart/%s" % (host, urllib.parse.quote(symbol, safe=""))
        try:
            async with session.get(url, params={"interval": "1d", "range": "1d"},
                                   headers={"User-Agent": "Mozilla/5.0"},
                                   timeout=aiohttp.ClientTimeout(total=6)) as r:
                body = await r.json()
            meta = (((body.get("chart") or {}).get("result") or [{}])[0] or {}).get("meta") or {}
            price = meta.get("regularMarketPrice")
            if price is not None and float(price) > 0:
                return meta
            err = "%s gave no price" % host
        except Exception as e:
            err = "%s: %s: %s" % (host, type(e).__name__, e)
        log.warning("stock %s %s", symbol, err)
    raise RuntimeError(err or "stock unreachable")


def _stock_line(key, meta):
    _symbol, name, unit = STOCK_INDEXES[key]
    price = float(meta["regularMarketPrice"])
    reg = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
    closed = not (reg.get("start") and reg.get("end")
                  and reg["start"] <= time.time() <= reg["end"])
    when = "取引時間中"
    if closed:
        when = "終値"
        mt = meta.get("regularMarketTime")
        if mt:
            t = time.gmtime(mt + (meta.get("gmtoffset") or 0))
            when = "%d月%d日の終値" % (t.tm_mon, t.tm_mday)
    move = ""
    prev = meta.get("chartPreviousClose")
    if prev is not None:
        d = price - float(prev)
        if abs(d) < 0.5:
            move = "、前日とほぼ同じ"
        else:
            move = "、前日より%s%s" % (_fmt_points(abs(d), unit),
                                      "高い" if d > 0 else "安い")
    return "%s: %s（%s）%s" % (name, _fmt_points(price, unit), when, move)


async def _stock_one(session, key):
    now = time.monotonic()
    hit = _stock_cache.get(key)
    if hit and now - hit[0] < STOCK_CACHE_TTL:
        ts, line = hit
    else:
        try:
            meta = await _stock_yahoo(session, STOCK_INDEXES[key][0])
            ts, line = now, _stock_line(key, meta)
            _stock_cache[key] = (ts, line)
        except Exception as e:
            log.warning("stock %s unavailable: %s: %s", key, type(e).__name__, e)
            if hit and now - hit[0] < STOCK_STALE_MAX:
                ts, line = hit
            else:
                return "error: いま%sを調べられませんでした（取得先が応答しません）" % (
                    STOCK_INDEXES[key][1])
    age = now - ts
    stale = " ※%d分前の情報" % int(age / 60) if age > 900 else ""
    return line + stale


async def get_stock_index(session, args, ctx=None) -> str:
    key = (args or {}).get("index")
    keys = [key] if key in STOCK_INDEXES else list(STOCK_INDEXES)
    lines = [await _stock_one(session, k) for k in keys]
    good = [x for x in lines if not x.startswith("error:")]
    return "。".join(good) if good else lines[0]


# ---- さくら無料枠の使用回数 -------------------------------------------------
# 利用量を返す API は無い（2026-08-01 実測: /v1/usage・quota・stats 等は全部 404、
# 応答ヘッダにもレート制限情報なし。コンパネの「利用量」はコンパネのセッション
# 認証で、API トークンからは見えない）。そこで、このサーバーから送った分を
# 自前で数える。検証などサーバーの外からの消費は数えられない＝読み上げでも断る。
SAKURA_QUOTA = int(os.environ.get("SAKURA_QUOTA", "3000"))
USAGE_PATH = os.environ.get(
    "LLM_USAGE_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "llm_usage.json"))


def _this_month() -> str:
    return datetime.datetime.now(JST).strftime("%Y-%m")


def _load_usage() -> dict:
    try:
        with open(USAGE_PATH) as f:
            d = json.load(f)
        if d.get("month") == _this_month() and isinstance(d.get("count"), int):
            return d
    except (OSError, ValueError):
        pass
    return {"month": _this_month(), "count": 0}


def count_llm_request() -> None:
    """さくらへの chat リクエストが 1 回成功するたびに app.py から呼ばれる。"""
    d = _load_usage()
    d["count"] += 1
    try:
        tmp = USAGE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, USAGE_PATH)
    except OSError as e:
        log.warning("llm usage not saved: %s", e)


async def get_llm_quota(session, args, ctx=None) -> str:
    d = _load_usage()
    used = d["count"]
    left = max(0, SAKURA_QUOTA - used)
    return ("今月このサーバーから使った LLM リクエストは%d回で、無料枠%d回の残りは"
            "およそ%d回です。サーバーの外で使った分（検証など）は数えられないので、"
            "正確な値はコントロールパネルの利用量が正です。" % (used, SAKURA_QUOTA, left))


# ---- 暗号資産（ビットコイン/イーサリアム） ---------------------------------
# 主は CoinGecko（キー不要・1 回で両方 + 24時間変動率）。予備は Yahoo Finance の
# BTC-JPY / ETH-JPY（_stock_yahoo を再利用。2026-08-01 の三点照合で CoinGecko と
# 一致）。Coinbase の spot は使わない（同日の照合で BTC-JPY が実勢の 3.6 倍の
# 異常値。為替でも板由来のずれを観測済みで、円建て暗号資産はさらに信用できない）。
CRYPTO_COINS = {
    "btc": ("bitcoin", "BTC-JPY", "ビットコイン"),
    "eth": ("ethereum", "ETH-JPY", "イーサリアム"),
}
CRYPTO_URL_GECKO = "https://api.coingecko.com/api/v3/simple/price"
# 取得先の障害で桁違いが来ても読まないための門番（円）
CRYPTO_SANE = {"btc": (1e5, 1e10), "eth": (1e3, 1e9)}
CRYPTO_CACHE_TTL = float(os.environ.get("CRYPTO_CACHE_TTL", "300"))
CRYPTO_STALE_MAX = float(os.environ.get("CRYPTO_STALE_MAX", "21600"))

_crypto_cache = {}   # coin キー -> (取得時刻 monotonic, 読み上げ文)


def _fmt_jpy_about(v) -> str:
    """9911652 -> 「991万円」。読み上げ用の丸め（1 円単位まで読まない）。"""
    v = float(v)
    if v >= 1e8:
        return "%.1f億円" % (v / 1e8)
    if v >= 1e4:
        return "%d万円" % round(v / 1e4)
    return "%d円" % round(v)


def _crypto_line(key, price, change_pct) -> str:
    name = CRYPTO_COINS[key][2]
    lo, hi = CRYPTO_SANE[key]
    price = float(price)
    if not lo <= price <= hi:
        raise RuntimeError("insane %s price: %r" % (key, price))
    move = ""
    if change_pct is not None:
        pct = round(float(change_pct))
        if abs(float(change_pct)) < 0.5:
            move = "、前日からほぼ横ばい"
        else:
            move = "、前日から%d%%%s" % (abs(pct), "高い" if pct > 0 else "安い")
    return "%s: およそ%s%s" % (name, _fmt_jpy_about(price), move)


async def _crypto_gecko(session) -> dict:
    """両コインぶんの読み上げ文を一度に作る。"""
    ids = ",".join(v[0] for v in CRYPTO_COINS.values())
    async with session.get(CRYPTO_URL_GECKO,
                           params={"ids": ids, "vs_currencies": "jpy",
                                   "include_24hr_change": "true"},
                           timeout=aiohttp.ClientTimeout(total=8)) as r:
        body = await r.json()
    out = {}
    for key, (gid, _sym, _name) in CRYPTO_COINS.items():
        d = body.get(gid) or {}
        if d.get("jpy") is not None:
            out[key] = _crypto_line(key, d["jpy"], d.get("jpy_24h_change"))
    if not out:
        raise RuntimeError("coingecko gave no price")
    return out


async def _crypto_one(session, key) -> str:
    now = time.monotonic()
    hit = _crypto_cache.get(key)
    if hit and now - hit[0] < CRYPTO_CACHE_TTL:
        ts, line = hit
    else:
        line = None
        try:
            for k, ln in (await _crypto_gecko(session)).items():
                _crypto_cache[k] = (now, ln)
            ts, line = _crypto_cache[key]
        except Exception as e:
            log.warning("crypto gecko failed: %s: %s", type(e).__name__, e)
        if line is None:
            try:
                meta = await _stock_yahoo(session, CRYPTO_COINS[key][1])
                price = float(meta["regularMarketPrice"])
                prev = meta.get("chartPreviousClose")
                pct = (price / float(prev) - 1) * 100 if prev else None
                ts, line = now, _crypto_line(key, price, pct)
                _crypto_cache[key] = (ts, line)
            except Exception as e:
                log.warning("crypto %s unavailable: %s: %s", key, type(e).__name__, e)
                if hit and now - hit[0] < CRYPTO_STALE_MAX:
                    ts, line = hit
                else:
                    return "error: いま%sの価格を調べられませんでした（取得先が応答しません）" % (
                        CRYPTO_COINS[key][2])
    age = now - ts
    stale = " ※%d分前の情報" % int(age / 60) if age > 900 else ""
    return line + stale


async def get_crypto(session, args, ctx=None) -> str:
    key = (args or {}).get("coin")
    keys = [key] if key in CRYPTO_COINS else list(CRYPTO_COINS)
    lines = [await _crypto_one(session, k) for k in keys]
    good = [x for x in lines if not x.startswith("error:")]
    return "。".join(good) if good else lines[0]


# ---- ニュース（NHK RSS） ----------------------------------------------------
# NHK の公開 RSS（cat0 = 主要ニュース）。リダイレクトされるので追従が必要
# （aiohttp は既定で追従）。応答整形が 2 文で切るため、見出しは読点で繋いだ
# 一文にして返す。
NEWS_URL = os.environ.get("NEWS_URL", "https://www.nhk.or.jp/rss/news/cat0.xml")
NEWS_COUNT = int(os.environ.get("NEWS_COUNT", "3"))
NEWS_CACHE_TTL = float(os.environ.get("NEWS_CACHE_TTL", "600"))
NEWS_STALE_MAX = float(os.environ.get("NEWS_STALE_MAX", "21600"))

_news_cache = []   # [(取得時刻 monotonic, [見出し])] を 1 件だけ


async def _news_fetch(session):
    async with session.get(NEWS_URL, headers={"User-Agent": "Mozilla/5.0"},
                           timeout=aiohttp.ClientTimeout(total=10)) as r:
        raw = await r.read()
    root = ET.fromstring(raw)
    titles = [t.text.strip() for t in root.iter("title") if t.text and t.text.strip()]
    titles = titles[1:]   # 先頭はチャンネル名
    if not titles:
        raise RuntimeError("no news items")
    return titles


async def get_news(session, args, ctx=None) -> str:
    now = time.monotonic()
    if _news_cache and now - _news_cache[0][0] < NEWS_CACHE_TTL:
        ts, titles = _news_cache[0]
    else:
        try:
            titles = await _news_fetch(session)
            ts = now
            _news_cache[:] = [(ts, titles)]
        except Exception as e:
            log.warning("news unavailable: %s: %s", type(e).__name__, e)
            if _news_cache and now - _news_cache[0][0] < NEWS_STALE_MAX:
                ts, titles = _news_cache[0]
            else:
                return "error: いまニュースを取れませんでした（取得先が応答しません）"
    age = now - ts
    stale = " ※%d分前の情報" % int(age / 60) if age > 900 else ""
    return "主なニュースは、" + "、".join(titles[:NEWS_COUNT]) + "、以上です。" + stale


# ---- 地震情報（気象庁） ------------------------------------------------------
# 気象庁の公開 JSON（bosai/quake）。新しい順に並んでいる。maxi が空の報は
# 震源情報のみ（震度なし）なので飛ばす。
QUAKE_URL = os.environ.get(
    "QUAKE_URL", "https://www.jma.go.jp/bosai/quake/data/list.json")
QUAKE_CACHE_TTL = float(os.environ.get("QUAKE_CACHE_TTL", "300"))
QUAKE_STALE_MAX = float(os.environ.get("QUAKE_STALE_MAX", "21600"))

_quake_cache = []   # [(取得時刻 monotonic, body)] を 1 件だけ
_INT_LABEL = {"5-": "5弱", "5+": "5強", "6-": "6弱", "6+": "6強"}


def _fmt_quake_time(at_iso: str, now=None) -> str:
    d = datetime.datetime.fromisoformat(at_iso).astimezone(JST)
    now = now or datetime.datetime.now(JST)
    if d.date() == now.date():
        day = "きょう"
    elif d.date() == now.date() - datetime.timedelta(days=1):
        day = "きのう"
    else:
        day = "%d月%d日" % (d.month, d.day)
    return "%s%d時%d分ごろ" % (day, d.hour, d.minute)


def _quake_line(quakes, now=None) -> str:
    q = quakes[0]
    shindo = _INT_LABEL.get(q["maxi"], q["maxi"])
    mag = q.get("mag")
    mtxt = "マグニチュード%s、" % mag if mag and "不明" not in str(mag) else ""
    line = "%s、%sで%s最大震度%sの地震がありました" % (
        _fmt_quake_time(q["at"], now), q.get("anm") or "震源不明", mtxt, shindo)
    day_ago = (now or datetime.datetime.now(JST)) - datetime.timedelta(days=1)
    recent = {x["eid"] for x in quakes
              if datetime.datetime.fromisoformat(x["at"]) >= day_ago}
    if len(recent) >= 2:
        line += "。この24時間では震度1以上の地震が%d回起きています" % len(recent)
    return line


async def get_quake(session, args, ctx=None) -> str:
    now = time.monotonic()
    if _quake_cache and now - _quake_cache[0][0] < QUAKE_CACHE_TTL:
        ts, body = _quake_cache[0]
    else:
        try:
            async with session.get(QUAKE_URL,
                                   timeout=aiohttp.ClientTimeout(total=10)) as r:
                body = await r.json()
            if not isinstance(body, list):
                raise RuntimeError("unexpected quake payload")
            ts = now
            _quake_cache[:] = [(ts, body)]
        except Exception as e:
            log.warning("quake unavailable: %s: %s", type(e).__name__, e)
            if _quake_cache and now - _quake_cache[0][0] < QUAKE_STALE_MAX:
                ts, body = _quake_cache[0]
            else:
                return "error: いま地震情報を取れませんでした（取得先が応答しません）"
    age = now - ts
    stale = " ※%d分前の情報" % int(age / 60) if age > 900 else ""
    quakes = [q for q in body if q.get("maxi") and q.get("at")]
    if not quakes:
        return "最近の震度のついた地震情報は見つかりませんでした" + stale
    return _quake_line(quakes) + stale


# ---- 気象の警報・注意報（気象庁） --------------------------------------------
# 気象庁の公開 JSON（bosai/warning）。都道府県単位で取り、status が
# 「発表」「継続」のものだけを有効とみなす。コード表は気象庁 XML の標準表。
WARNING_URL_BASE = "https://www.jma.go.jp/bosai/warning/data/warning/"
WARNING_AREA_CODE = os.environ.get("WARNING_AREA_CODE", "140000")
WARNING_AREA_NAME = os.environ.get("WARNING_AREA_NAME", "神奈川県")
WARNING_CACHE_TTL = float(os.environ.get("WARNING_CACHE_TTL", "300"))
WARNING_STALE_MAX = float(os.environ.get("WARNING_STALE_MAX", "21600"))

_warning_cache = []   # [(取得時刻 monotonic, body)] を 1 件だけ

# 重い順（読み上げ順にもなる）
_WARNING_NAMES = {
    "33": "大雨特別警報", "35": "暴風特別警報", "32": "暴風雪特別警報",
    "36": "大雪特別警報", "37": "波浪特別警報", "38": "高潮特別警報",
    "03": "大雨警報", "04": "洪水警報", "05": "暴風警報", "02": "暴風雪警報",
    "06": "大雪警報", "07": "波浪警報", "08": "高潮警報",
    "10": "大雨注意報", "18": "洪水注意報", "15": "強風注意報", "13": "風雪注意報",
    "14": "雷注意報", "12": "大雪注意報", "16": "波浪注意報", "19": "高潮注意報",
    "20": "濃霧注意報", "21": "乾燥注意報", "22": "なだれ注意報", "23": "低温注意報",
    "24": "霜注意報", "17": "融雪注意報", "25": "着氷注意報", "26": "着雪注意報",
}


def _active_warnings(body) -> list:
    codes = set()
    for at in body.get("areaTypes") or []:
        for area in at.get("areas") or []:
            for w in area.get("warnings") or []:
                if w.get("status") in ("発表", "継続") and w.get("code"):
                    codes.add(str(w["code"]))
    unknown = codes - set(_WARNING_NAMES)
    if unknown:
        log.warning("unknown warning codes: %s", sorted(unknown))
    return [name for code, name in _WARNING_NAMES.items() if code in codes]


async def get_warning(session, args, ctx=None) -> str:
    now = time.monotonic()
    if _warning_cache and now - _warning_cache[0][0] < WARNING_CACHE_TTL:
        ts, body = _warning_cache[0]
    else:
        try:
            url = WARNING_URL_BASE + WARNING_AREA_CODE + ".json"
            async with session.get(url,
                                   timeout=aiohttp.ClientTimeout(total=10)) as r:
                body = await r.json()
            if not isinstance(body, dict):
                raise RuntimeError("unexpected warning payload")
            ts = now
            _warning_cache[:] = [(ts, body)]
        except Exception as e:
            log.warning("warning unavailable: %s: %s", type(e).__name__, e)
            if _warning_cache and now - _warning_cache[0][0] < WARNING_STALE_MAX:
                ts, body = _warning_cache[0]
            else:
                return "error: いま警報・注意報の情報を取れませんでした（取得先が応答しません）"
    age = now - ts
    stale = " ※%d分前の情報" % int(age / 60) if age > 900 else ""
    names = _active_warnings(body)
    if not names:
        return "いま%sに警報・注意報は出ていません" % WARNING_AREA_NAME + stale
    return "%sに%sが出ています" % (WARNING_AREA_NAME, "、".join(names)) + stale


# ---- 台風情報（気象庁） ------------------------------------------------------
# targetTc.json に発生中の台風一覧、各 TC の specifications.json に詳細。
# part が "title" の要素に番号と名前、part.jp が「実況」の要素に現況が入る。
# typhoonNumber が空の要素は台風未満（熱帯低気圧）なので飛ばす。
TYPHOON_LIST_URL = os.environ.get(
    "TYPHOON_LIST_URL",
    "https://www.jma.go.jp/bosai/typhoon/data/targetTc.json")
TYPHOON_SPEC_URL = os.environ.get(
    "TYPHOON_SPEC_URL",
    "https://www.jma.go.jp/bosai/typhoon/data/%s/specifications.json")
TYPHOON_CACHE_TTL = float(os.environ.get("TYPHOON_CACHE_TTL", "600"))
TYPHOON_STALE_MAX = float(os.environ.get("TYPHOON_STALE_MAX", "21600"))
TYPHOON_MAX = int(os.environ.get("TYPHOON_MAX", "2"))

_typhoon_cache = []   # [(取得時刻 monotonic, line)] を 1 件だけ


def _typhoon_one(spec) -> str:
    title = next((x for x in spec if x.get("part") == "title"), {})
    ana = next((x for x in spec
                if isinstance(x.get("part"), dict)
                and x["part"].get("jp") == "実況"), {})
    num = str(title.get("typhoonNumber") or "")
    name = (title.get("name") or {}).get("jp") or ""
    n = int(num[-2:]) if num[-2:].isdigit() else 0
    head = "台風%d号%s" % (n, name) if n else "台風%s" % name
    bits = [head + "が発生しています。"]
    loc = str(ana.get("location") or "")
    if loc:
        s = "現在%sにあって" % loc
        inten = str(ana.get("intensity") or "")
        if inten and inten != "-":
            s += "、%s勢力です" % inten
        else:
            s += "います"
        bits.append(s + "。")
    course = str(ana.get("course") or "")
    speed = str((ana.get("speed") or {}).get("km/h") or "")
    if course and speed.isdigit():
        bits.append("%sへ時速%dキロで進んでいます。" % (course, int(speed)))
    elif course:
        bits.append("%sへゆっくり進んでいます。" % course)
    pres = str(ana.get("pressure") or "")
    if pres.isdigit():
        bits.append("中心気圧は%dヘクトパスカルです。" % int(pres))
    return "".join(bits)


async def _typhoon_fetch(session):
    tmo = aiohttp.ClientTimeout(total=10)
    async with session.get(TYPHOON_LIST_URL, timeout=tmo) as r:
        tcs = json.loads(await r.read())
    tcs = [t for t in tcs
           if t.get("tropicalCyclone") and str(t.get("typhoonNumber") or "")]
    if not tcs:
        return "いま発生している台風はありません。"
    lines = []
    for tc in tcs[:TYPHOON_MAX]:
        async with session.get(TYPHOON_SPEC_URL % tc["tropicalCyclone"],
                               timeout=tmo) as r:
            spec = json.loads(await r.read())
        lines.append(_typhoon_one(spec))
    if len(tcs) > TYPHOON_MAX:
        lines.append("ほかにも台風が%d個あります。" % (len(tcs) - TYPHOON_MAX))
    return "".join(lines)


async def get_typhoon(session, args, ctx=None) -> str:
    now = time.monotonic()
    if _typhoon_cache and now - _typhoon_cache[0][0] < TYPHOON_CACHE_TTL:
        ts, line = _typhoon_cache[0]
    else:
        try:
            line = await _typhoon_fetch(session)
            ts = now
            _typhoon_cache[:] = [(ts, line)]
        except Exception as e:
            log.warning("typhoon unavailable: %s: %s", type(e).__name__, e)
            if _typhoon_cache and now - _typhoon_cache[0][0] < TYPHOON_STALE_MAX:
                ts, line = _typhoon_cache[0]
            else:
                return "error: いま台風情報を取れませんでした（取得先が応答しません）"
    age = now - ts
    stale = " ※%d分前の情報" % int(age / 60) if age > 900 else ""
    return line + stale


# ---- 熱中症（環境省 暑さ指数 WBGT） ------------------------------------------
# 予測値 CSV: 1 行目が YYYYMMDDHH のヘッダ、2 行目が地点の値（WBGT×10）。
# 警戒アラート CSV: 都道府県ごとの行に当日/翌日のフラグ（0=無し, 1=警戒,
# 2/3=特別警戒, 9=発表時間外）。地点番号は既定で横浜（46106）。
HEAT_FCST_URL = os.environ.get(
    "HEAT_FCST_URL", "https://www.wbgt.env.go.jp/prev15WG/dl/yohou_%s.csv")
HEAT_ALERT_URL = os.environ.get(
    "HEAT_ALERT_URL", "https://www.wbgt.env.go.jp/alert/dl/%s/alert_%s_%s.csv")
HEAT_POINT = os.environ.get("HEAT_POINT", "46106")
HEAT_PREF = os.environ.get("HEAT_PREF", "神奈川県")
# 環境省の予測値提供は夏期だけ（令和8年度は 4月22日〜10月21日）。
# 期間外は取得先が落ちているのではないので、言い方を分ける。
HEAT_SEASON_FROM = os.environ.get("HEAT_SEASON_FROM", "04-22")
HEAT_SEASON_TO = os.environ.get("HEAT_SEASON_TO", "10-21")
HEAT_CACHE_TTL = float(os.environ.get("HEAT_CACHE_TTL", "1800"))
HEAT_STALE_MAX = float(os.environ.get("HEAT_STALE_MAX", "21600"))

_heat_cache = []   # [(取得時刻 monotonic, line)] を 1 件だけ
_ALERT_FLAG = {"1": "熱中症警戒アラート", "2": "熱中症特別警戒アラート",
               "3": "熱中症特別警戒アラート"}


def _wbgt_level(w: float) -> str:
    if w >= 31:
        return "危険"
    if w >= 28:
        return "厳重警戒"
    if w >= 25:
        return "警戒"
    if w >= 21:
        return "注意"
    return "ほぼ安全"


def _wbgt_advice(level: str) -> str:
    return {
        "危険": "外に出るのは避けて、涼しいところにいてください。",
        "厳重警戒": "外での運動は控えて、こまめに水分をとってください。",
        "警戒": "運動するときは休憩をこまめに入れてください。",
        "注意": "激しい運動のときは水分補給を忘れずに。",
    }.get(level, "")


def _heat_pick_now(rows, now):
    """予測 CSV から今の時刻に一番近い（過去寄りの）値を返す。"""
    stamp = now.strftime("%Y%m%d%H")
    best = None
    for key, val in rows:
        if key <= stamp or best is None:
            best = (key, val)
        if key > stamp:
            break
    return best


def _heat_parse_fcst(text, now):
    lines = [x for x in text.splitlines() if x.strip()]
    if len(lines) < 2:
        raise RuntimeError("wbgt csv too short")
    head = [x.strip() for x in lines[0].split(",")]
    vals = [x.strip() for x in lines[1].split(",")]
    rows = [(head[i], vals[i]) for i in range(2, min(len(head), len(vals)))
            if head[i].isdigit() and vals[i].lstrip("-").isdigit()]
    if not rows:
        raise RuntimeError("wbgt csv has no values")
    now_row = _heat_pick_now(rows, now)
    today = now.strftime("%Y%m%d")
    todays = [int(v) for k, v in rows if k.startswith(today)]
    peak = max(todays) / 10.0 if todays else int(now_row[1]) / 10.0
    return int(now_row[1]) / 10.0, peak


def _heat_parse_alert(text, pref):
    for line in text.splitlines():
        cells = [x.strip() for x in line.split(",")]
        if cells and cells[0] == pref and len(cells) > 8:
            return cells[6], cells[7]
    return None, None


def _heat_alert_versions(now):
    """見るべき（日付, 版）の候補。17時版は翌日の発表を含み、5時版は当日分。
    未明は当日の5時版がまだ無いので前日の17時版に落ちる。"""
    d = now.date()
    prev = d - datetime.timedelta(days=1)
    if now.hour >= 17:
        return [(d, "17"), (d, "05")]
    if now.hour >= 5:
        return [(d, "05"), (prev, "17")]
    return [(prev, "17"), (d, "05")]


async def _heat_fetch(session, now):
    tmo = aiohttp.ClientTimeout(total=10)
    async with session.get(HEAT_FCST_URL % HEAT_POINT, timeout=tmo) as r:
        cur, peak = _heat_parse_fcst((await r.read()).decode("utf-8", "replace"), now)
    bits = ["いまの%sの暑さ指数はおよそ%.0fで、%sレベルです。"
            % (HEAT_PREF, cur, _wbgt_level(cur))]
    if peak > cur + 0.5:
        bits.append("きょうはこのあと%.0fまで上がる予想です。" % peak)
    lv = _wbgt_level(max(cur, peak))
    today_f = tomo_f = None
    for day, ver in _heat_alert_versions(now):
        url = HEAT_ALERT_URL % (day.strftime("%Y"), day.strftime("%Y%m%d"), ver)
        try:
            async with session.get(url, timeout=tmo) as r:
                if r.status != 200:
                    continue
                today_f, tomo_f = _heat_parse_alert(
                    (await r.read()).decode("utf-8", "replace"), HEAT_PREF)
        except Exception as e:
            log.warning("heat alert unavailable: %s: %s", type(e).__name__, e)
            continue
        if today_f is not None:
            break
    if today_f in _ALERT_FLAG:
        bits.append("きょうは%sに%sが出ています。" % (HEAT_PREF, _ALERT_FLAG[today_f]))
    elif tomo_f in _ALERT_FLAG:
        bits.append("あすは%sが出ています。" % _ALERT_FLAG[tomo_f])
    adv = _wbgt_advice(lv)
    if adv:
        bits.append(adv)
    return "".join(bits)


def _heat_in_season(d) -> bool:
    return HEAT_SEASON_FROM <= d.strftime("%m-%d") <= HEAT_SEASON_TO


async def get_heat(session, args, ctx=None) -> str:
    if not _heat_in_season(now_jst()):
        return ("いまの時期は暑さ指数の情報が出ていません。"
                "環境省の提供は毎年4月下旬から10月下旬までです。")
    now = time.monotonic()
    if _heat_cache and now - _heat_cache[0][0] < HEAT_CACHE_TTL:
        ts, line = _heat_cache[0]
    else:
        try:
            line = await _heat_fetch(session, now_jst())
            ts = now
            _heat_cache[:] = [(ts, line)]
        except Exception as e:
            log.warning("heat unavailable: %s: %s", type(e).__name__, e)
            if _heat_cache and now - _heat_cache[0][0] < HEAT_STALE_MAX:
                ts, line = _heat_cache[0]
            else:
                return "error: いま暑さ指数を取れませんでした（取得先が応答しません）"
    age = now - ts
    stale = " ※%d分前の情報" % int(age / 60) if age > 1800 else ""
    return line + stale


# ---- 今日は何の日（Wikipedia） ----------------------------------------------
# 「Wikipedia:今日は何の日 N月」の wikitext を取り、"== [[N月D日]] ==" 節の
# 箇条書きを拾う。UA を付けないと 403 になる。読み上げ用にリンク記法
# [[表示|実体]] や注記の括弧を落として、年号だけ残す。
ONTHISDAY_URL = os.environ.get(
    "ONTHISDAY_URL",
    "https://ja.wikipedia.org/w/api.php?action=parse&page="
    "Wikipedia:%E4%BB%8A%E6%97%A5%E3%81%AF%E4%BD%95%E3%81%AE%E6%97%A5%20"
    "{month}%E6%9C%88&prop=wikitext&format=json&formatversion=2")
ONTHISDAY_COUNT = int(os.environ.get("ONTHISDAY_COUNT", "3"))
ONTHISDAY_MAX_CHARS = int(os.environ.get("ONTHISDAY_MAX_CHARS", "110"))
ONTHISDAY_CACHE_TTL = float(os.environ.get("ONTHISDAY_CACHE_TTL", "21600"))

_onthisday_cache = {}   # {"MM-DD": (取得時刻 monotonic, [出来事])}


def _wiki_plain(s: str) -> str:
    """wikitext の 1 行を読み上げ向けの素の文にする。"""
    out = []
    i = 0
    while i < len(s):
        if s.startswith("[[", i):
            j = s.find("]]", i)
            if j < 0:
                break
            inner = s[i + 2:j]
            # [[実体|表示]] は表示側を読む
            out.append(inner.split("|")[-1] if "|" in inner else inner)
            i = j + 2
            continue
        if s.startswith("{{", i):
            depth, i = 1, i + 2
            while i < len(s) and depth:
                if s.startswith("{{", i):
                    depth, i = depth + 1, i + 2
                elif s.startswith("}}", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            continue
        if s[i] == "<":
            j = s.find(">", i)
            i = (j + 1) if j >= 0 else len(s)
            continue
        if s[i] == "'" and s.startswith("''", i):
            i += 2
            continue
        out.append(s[i])
        i += 1
    t = "".join(out).replace(" (旧暦)", "").strip()
    return " ".join(t.split())


def _onthisday_year(item: str) -> str:
    """末尾の（1936年）などから年だけ拾う。無ければ空。"""
    a = item.rfind("（")
    if a < 0:
        return ""
    tail = item[a + 1:].rstrip("）")
    head = tail.split(" - ")[0].strip()
    return head if head.endswith("年") else ""


def _onthisday_parse(wikitext: str, month: int, day: int) -> list:
    head = "== [[%d月%d日]] ==" % (month, day)
    i = wikitext.find(head)
    if i < 0:
        raise RuntimeError("no section for %d/%d" % (month, day))
    body = wikitext[i + len(head):]
    j = body.find(chr(10) + "== ")
    if j >= 0:
        body = body[:j]
    events = []
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("* "):
            continue
        plain = _wiki_plain(line[2:])
        if not plain:
            continue
        year = _onthisday_year(plain)
        a = plain.rfind("（")
        text = (plain[:a] if a > 0 else plain).strip("：: ")
        if not text:
            continue
        events.append(("%sに%s" % (year, text)) if year else text)
    if not events:
        raise RuntimeError("no events for %d/%d" % (month, day))
    return events


async def _onthisday_fetch(session, month: int, day: int) -> list:
    url = ONTHISDAY_URL.replace("{month}", str(month))
    async with session.get(url, headers={"User-Agent": "stackchan-server/1.0"},
                           timeout=aiohttp.ClientTimeout(total=10)) as r:
        data = json.loads(await r.read())
    if "parse" not in data:
        raise RuntimeError("wikipedia: %s" % str(data.get("error"))[:80])
    return _onthisday_parse(data["parse"]["wikitext"], month, day)


async def get_onthisday(session, args, ctx=None) -> str:
    d = now_jst()
    key = "%02d-%02d" % (d.month, d.day)
    now = time.monotonic()
    hit = _onthisday_cache.get(key)
    if hit and now - hit[0] < ONTHISDAY_CACHE_TTL:
        events = hit[1]
    else:
        try:
            events = await _onthisday_fetch(session, d.month, d.day)
            _onthisday_cache[key] = (now, events)
            _trim(_onthisday_cache)
        except Exception as e:
            log.warning("onthisday unavailable: %s: %s", type(e).__name__, e)
            if hit:
                events = hit[1]
            else:
                return "error: いま今日は何の日を調べられませんでした（取得先が応答しません）"
    picks = events[-ONTHISDAY_COUNT:] if len(events) > ONTHISDAY_COUNT else events
    head = "%d月%d日にあった出来事は、" % (d.month, d.day)
    # 長い日は件数を減らす（応答整形が 160 字で切るため、途中で尻切れにしない）
    while len(picks) > 1 and len(head + "、".join(picks)) + 5 > ONTHISDAY_MAX_CHARS:
        picks = picks[1:]
    return head + "、".join(picks) + "、などです。"


# ---- 月齢・日の出日の入り（計算のみ・外部通信なし） --------------------------
# 日の出入りは NOAA の簡易式、月齢は既知の新月（2000-01-06 18:14 UTC）からの
# 朔望月周期で求める。どちらも数分の誤差があるが読み上げには十分。
SKY_LAT = float(os.environ.get("SKY_LAT", "35.4437"))    # 横浜
SKY_LON = float(os.environ.get("SKY_LON", "139.6380"))
SKY_PLACE = os.environ.get("SKY_PLACE", "横浜")
SYNODIC = 29.530588853
NEW_MOON_EPOCH = datetime.datetime(2000, 1, 6, 18, 14,
                                   tzinfo=datetime.timezone.utc)


def _moon_age(d: datetime.datetime) -> float:
    """月齢（日）。太陽との離角から出す（平均朔望月だけだと最大 0.5 日ずれる）。
    Meeus の短縮級数で、実測の朔望とは 0.1 日ほどの差に収まる。"""
    ts = d.astimezone(datetime.timezone.utc).timestamp()
    jd = ts / 86400.0 + 2440587.5
    t = (jd - 2451545.0) / 36525.0
    dd = 297.8501921 + 445267.1114034 * t - 0.0018819 * t * t
    ms = 357.5291092 + 35999.0502909 * t - 0.0001536 * t * t
    mm = 134.9633964 + 477198.8675055 * t + 0.0087414 * t * t
    r = math.radians
    elong = (dd
             + 6.289 * math.sin(r(mm))
             - 2.100 * math.sin(r(ms))
             - 1.274 * math.sin(r(2 * dd - mm))
             - 0.658 * math.sin(r(2 * dd))
             - 0.214 * math.sin(r(2 * mm))
             - 0.110 * math.sin(r(dd)))
    return (elong % 360.0) / 360.0 * SYNODIC


def _moon_name(age: float) -> str:
    if age < 1.5 or age >= 28.5:
        return "新月ごろ"
    if age < 5.5:
        return "細い三日月"
    if age < 9.5:
        return "上弦の半月ごろ"
    if age < 13.5:
        return "満月に向かって満ちていく月"
    if age < 16.5:
        return "満月ごろ"
    if age < 20.5:
        return "満月をすぎて欠けはじめた月"
    if age < 24.5:
        return "下弦の半月ごろ"
    return "明け方に見える細い月"


def _sun_events(d: datetime.datetime):
    """その日の日の出・日の入り（JST の datetime）。極夜等は None。"""
    day = d.astimezone(JST).date()
    base = datetime.date(2000, 1, 1)
    n = (day - base).days + 0.0008 - SKY_LON / 360.0
    out = []
    for rising in (True, False):
        m = 357.5291 + 0.98560028 * n
        c = (1.9148 * math.sin(math.radians(m))
             + 0.02 * math.sin(math.radians(2 * m))
             + 0.0003 * math.sin(math.radians(3 * m)))
        lam = math.radians((m + c + 180 + 102.9372) % 360)
        j_transit = (2451545.0 + n
                     + 0.0053 * math.sin(math.radians(m))
                     - 0.0069 * math.sin(2 * lam))
        dec = math.asin(math.sin(lam) * math.sin(math.radians(23.44)))
        lat = math.radians(SKY_LAT)
        cos_w = ((math.sin(math.radians(-0.833)) - math.sin(lat) * math.sin(dec))
                 / (math.cos(lat) * math.cos(dec)))
        if abs(cos_w) > 1:
            out.append(None)
            continue
        w = math.degrees(math.acos(cos_w)) / 360.0
        j = j_transit + (-w if rising else w)
        secs = (j - 2440587.5) * 86400.0
        out.append(datetime.datetime.fromtimestamp(secs, datetime.timezone.utc)
                   .astimezone(JST))
    return out[0], out[1]


async def get_sky(session, args, ctx=None) -> str:
    d = now_jst()
    age = _moon_age(d)
    bits = ["きょうの月齢はおよそ%.1fで、%sです。" % (age, _moon_name(age))]
    try:
        rise, setting = _sun_events(d)
    except Exception as e:
        log.warning("sun calc failed: %s: %s", type(e).__name__, e)
        rise = setting = None
    if rise and setting:
        bits.append("%sの日の出は%d時%d分、日の入りは%d時%d分です。"
                    % (SKY_PLACE, rise.hour, rise.minute,
                       setting.hour, setting.minute))
        if d < rise:
            left = (rise - d).total_seconds() / 60.0
            bits.append("日の出まであと%d分です。" % int(left))
        elif d < setting:
            left = (setting - d).total_seconds() / 60.0
            if left < 90:
                bits.append("日の入りまであと%d分です。" % int(left))
            else:
                bits.append("日の入りまであと%.1f時間です。" % (left / 60.0))
    return "".join(bits)


# ---- 電車の遅延（公共交通オープンデータセンター ODPT） -----------------------
# キー不要の api-public は都営地下鉄のみ。無料の consumer key（odpt.org で登録）を
# ODPT_TOKEN に入れると JR東日本・京急・東急・相鉄・東京メトロ・
# 横浜市営地下鉄も読める（横浜シーサイドラインは ODPT に無い。
# ckan.odpt.org カタログ実確認 2026-08-02）。
# 平常時は odpt:trainInformationStatus が無く、異常時だけ入る。
ODPT_TOKEN = os.environ.get("ODPT_TOKEN", "").strip()


def _hide_token(s: str) -> str:
    """例外文字列には URL がそのまま入る（aiohttp）。トークンを伏せてから記録する。"""
    return s.replace(ODPT_TOKEN, "***") if ODPT_TOKEN else s
ODPT_PUBLIC_URL = os.environ.get(
    "ODPT_PUBLIC_URL",
    "https://api-public.odpt.org/api/v4/odpt:TrainInformation")
ODPT_KEYED_URL = os.environ.get(
    "ODPT_KEYED_URL", "https://api.odpt.org/api/v4/odpt:TrainInformation")
# 並び順が読み上げの優先順（user 指定: 京急がデフォルト 2026-08-02）
TRAIN_OPERATORS = os.environ.get(
    "TRAIN_OPERATORS",
    "Keikyu,JR-East,YokohamaMunicipal,Tokyu,Sotetsu,TokyoMetro,Toei")
TRAIN_CACHE_TTL = float(os.environ.get("TRAIN_CACHE_TTL", "180"))
TRAIN_STALE_MAX = float(os.environ.get("TRAIN_STALE_MAX", "3600"))
TRAIN_MAX_LINES = int(os.environ.get("TRAIN_MAX_LINES", "3"))

_train_cache = []   # [(取得時刻 monotonic, line)] を 1 件だけ

# 横浜まわりで名前が出うる路線だけ読み方を持つ。未知の路線は英語 ID の
# 末尾をそのまま読ませず「一部の路線」に丸める。
TRAIN_LINE_NAMES = {
    "JR-East.Tokaido": "JRの東海道線",
    "JR-East.KeihinTohokuNegishi": "JRの京浜東北・根岸線",
    "JR-East.Yokosuka": "JRの横須賀線",
    "JR-East.ShonanShinjuku": "湘南新宿ライン",
    "JR-East.UenoTokyo": "上野東京ライン",
    "JR-East.Yokohama": "JRの横浜線",
    "JR-East.Nambu": "JRの南武線",
    "JR-East.Tsurumi": "JRの鶴見線",
    "JR-East.Sagami": "JRの相模線",
    "JR-East.Yamanote": "JRの山手線",
    "JR-East.ChuoRapid": "JRの中央線",
    "JR-East.Sobu": "JRの総武線",
    "JR-East.Saikyo": "JRの埼京線",
    "JR-East.Takasaki": "JRの高崎線",
    "JR-East.Utsunomiya": "JRの宇都宮線",
    "JR-East.Joban": "JRの常磐線",
    "JR-East.Musashino": "JRの武蔵野線",
    "JR-East.Keiyo": "JRの京葉線",
    "Keikyu.Main": "京急本線",
    "Keikyu.Airport": "京急空港線",
    "Keikyu.Kurihama": "京急久里浜線",
    "Keikyu.Zushi": "京急逗子線",
    "Keikyu.Daishi": "京急大師線",
    "Tokyu.Toyoko": "東急東横線",
    "Tokyu.Meguro": "東急目黒線",
    "Tokyu.DenEnToshi": "東急田園都市線",
    "Tokyu.Oimachi": "東急大井町線",
    "Tokyu.Ikegami": "東急池上線",
    "Tokyu.TokyuTamagawa": "東急多摩川線",
    "Tokyu.Kodomonokuni": "こどもの国線",
    "Tokyu.ShinYokohama": "東急新横浜線",
    "Sotetsu.Main": "相鉄本線",
    "Sotetsu.Izumino": "相鉄いずみ野線",
    "Sotetsu.SotetsuShinYokohama": "相鉄新横浜線",
    "TokyoMetro.Ginza": "地下鉄銀座線",
    "TokyoMetro.Marunouchi": "地下鉄丸ノ内線",
    "TokyoMetro.Hibiya": "地下鉄日比谷線",
    "TokyoMetro.Tozai": "地下鉄東西線",
    "TokyoMetro.Chiyoda": "地下鉄千代田線",
    "TokyoMetro.Yurakucho": "地下鉄有楽町線",
    "TokyoMetro.Hanzomon": "地下鉄半蔵門線",
    "TokyoMetro.Namboku": "地下鉄南北線",
    "TokyoMetro.Fukutoshin": "地下鉄副都心線",
    "Toei.Asakusa": "都営浅草線",
    "Toei.Mita": "都営三田線",
    "Toei.Shinjuku": "都営新宿線",
    "Toei.Oedo": "都営大江戸線",
    "Toei.Arakawa": "都電荒川線",
    "Toei.NipporiToneri": "日暮里・舎人ライナー",
    # Railway ID はキー到着後の実データで要確認（違っても名前が引けない
    # だけで「一部の路線」に丸まる。落ちない）
    "YokohamaMunicipal.Blue": "横浜市営地下鉄ブルーライン",
    "YokohamaMunicipal.Green": "横浜市営地下鉄グリーンライン",
}


def _train_line_name(rid: str) -> str:
    key = str(rid or "").replace("odpt.Railway:", "")
    return TRAIN_LINE_NAMES.get(key, "")


_TRAIN_OPS = [o.strip() for o in TRAIN_OPERATORS.split(",") if o.strip()]


def _train_rank(rid) -> int:
    """身近な事業者から読む（TRAIN_OPERATORS の並び順。京急が先頭）。"""
    op = str(rid or "").replace("odpt.Railway:", "").split(".")[0]
    return _TRAIN_OPS.index(op) if op in _TRAIN_OPS else len(_TRAIN_OPS)


def _train_line(items, keyed: bool) -> str:
    scope = "東京・神奈川の主な路線" if keyed else "都営地下鉄"
    bad = []
    for e in items:
        st = (e.get("odpt:trainInformationStatus") or {})
        st = st.get("ja") if isinstance(st, dict) else st
        if not st:
            continue
        name = _train_line_name(e.get("odpt:railway"))
        cause = (e.get("odpt:trainInformationCause") or {})
        cause = cause.get("ja") if isinstance(cause, dict) else cause
        bad.append((_train_rank(e.get("odpt:railway")), name,
                    str(st), str(cause or "")))
    bad.sort(key=lambda b: b[0])
    if not bad:
        line = "いま%sに大きな遅れは出ていません。" % scope
        if not keyed:
            line += "いまは都営地下鉄しか調べられません。"
        return line
    named = [b for b in bad if b[1]]
    shown = named[:TRAIN_MAX_LINES] if named else bad[:TRAIN_MAX_LINES]
    parts = ["%s%s" % (n or "一部の路線", "は" + s) for _, n, s, _ in shown]
    head = "いま、" + "、".join(parts) + "です。"
    rest = len(bad) - len(shown)
    if rest > 0:
        head += "ほかにも%d路線で乱れが出ています。" % rest
    cause = next((c for _, _, _, c in shown if c), "")
    if cause:
        head += "原因は%sです。" % cause
    return head


async def _train_fetch(session):
    tmo = aiohttp.ClientTimeout(total=10)
    if ODPT_TOKEN:
        ops = ",".join("odpt.Operator:" + o.strip()
                       for o in TRAIN_OPERATORS.split(",") if o.strip())
        url = "%s?odpt:operator=%s&acl:consumerKey=%s" % (
            ODPT_KEYED_URL, urllib.parse.quote(ops, safe=":,"),
            urllib.parse.quote(ODPT_TOKEN, safe=""))
        try:
            async with session.get(url, timeout=tmo) as r:
                items = json.loads(await r.read())
            if isinstance(items, list) and items:
                return _train_line(items, True)
            log.warning("odpt keyed returned nothing; falling back to public")
        except Exception as e:
            # 鍵切れ等でキー付きが死んでも、都営だけは公開分で答える
            log.warning("odpt keyed failed: %s: %s", type(e).__name__,
                        _hide_token(str(e))[:120])
    async with session.get(ODPT_PUBLIC_URL, timeout=tmo) as r:
        items = json.loads(await r.read())
    return _train_line(items, False)


async def get_train(session, args, ctx=None) -> str:
    now = time.monotonic()
    if _train_cache and now - _train_cache[0][0] < TRAIN_CACHE_TTL:
        ts, line = _train_cache[0]
    else:
        try:
            line = await _train_fetch(session)
            ts = now
            _train_cache[:] = [(ts, line)]
        except Exception as e:
            log.warning("train unavailable: %s: %s", type(e).__name__,
                        _hide_token(str(e))[:120])
            if _train_cache and now - _train_cache[0][0] < TRAIN_STALE_MAX:
                ts, line = _train_cache[0]
            else:
                return "error: いま運行情報を取れませんでした（取得先が応答しません）"
    age = now - ts
    stale = " ※%d分前の情報" % int(age / 60) if age > 600 else ""
    return line + stale


# ---- 燃油サーチャージ（ANA 公式ページ） --------------------------------------
# 公式の API は無い。ANA の燃油特別付加運賃ページ（静的 HTML・UA 必須）から、
# きょうの発券日に効く期間の「日本発」表を読む。JAL 公式は bot 遮断（403 実測
# 2026-08-02）で取れないため、ANA 基準の目安として読み上げる。改定は 2 か月ごと
FUEL_URL = os.environ.get(
    "FUEL_URL", "https://www.ana.co.jp/ja/jp/guide/plan/charge/fuelsurcharge/")
FUEL_CACHE_TTL = float(os.environ.get("FUEL_CACHE_TTL", "43200"))   # 12 時間
FUEL_STALE_MAX = float(os.environ.get("FUEL_STALE_MAX", "604800"))  # 7 日
FUEL_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
_fuel_cache = []   # [(取得時刻 monotonic, html)] を 1 件だけ

# 行き先の言い方 → 表の行ラベルに入っている語。ドバイ経由の旅行が user の
# 主用途なので、行き先を言われなければ中東
FUEL_ZONES = [
    ("中東", ("中東", "ドバイ", "アラブ", "UAE", "トルコ", "カタール", "ドーハ")),
    ("欧州", ("欧州", "ヨーロッパ", "パリ", "ロンドン", "フランクフルト", "ローマ")),
    ("北米", ("北米", "アメリカ", "ニューヨーク", "ロサンゼルス", "シカゴ", "カナダ")),
    ("オセアニア", ("オセアニア", "オーストラリア", "シドニー", "ニュージーランド")),
    ("ハワイ", ("ハワイ", "ホノルル")),
    ("インドネシア", ("インドネシア", "バリ", "ジャカルタ")),
    ("インド", ("インド",)),
    ("タイ", ("タイ", "バンコク")),
    ("シンガポール", ("シンガポール",)),
    ("マレーシア", ("マレーシア", "クアラルンプール")),
    ("ベトナム", ("ベトナム", "ハノイ", "ホーチミン")),
    ("グアム", ("グアム",)),
    ("フィリピン", ("フィリピン", "マニラ", "セブ")),
    ("東アジア", ("東アジア", "中国", "上海", "北京", "台湾", "台北", "香港")),
    ("韓国", ("韓国", "ソウル")),
]
_FUEL_PERIOD_RE = re.compile(
    r"運賃額[^0-9<>]{0,6}(\d{4})年(\d{1,2})月(\d{1,2})日から"
    r"(\d{4})年(\d{1,2})月(\d{1,2})日ご購入分まで")


def _fuel_pick(html, today):
    """きょうの発券日に効く期間を選び (期間の言い方, 日本発の表HTML) を返す。

    きょうを含む期間が無ければ（改定の谷間）、終了日が一番先の表を使う。
    期間見出しの直後の 1 つ目の表が「旅行開始国が日本」の表（実ページ確認済み）。
    """
    marks = list(_FUEL_PERIOD_RE.finditer(html))
    best = None
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(html)
        d2 = datetime.date(int(m.group(4)), int(m.group(5)), int(m.group(6)))
        d1 = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        key = (d1 <= today <= d2, d2)
        if best is None or key > best[0]:
            best = (key, html[m.start():end], d2)
    if best is None:
        return "", ""
    _, seg, d2 = best
    label = "%d年%d月%d日ご購入分まで" % (d2.year, d2.month, d2.day)
    t = re.search(r"<table.*?</table>", seg, re.S)
    return label, (t.group(0) if t else "")


def _fuel_amount(table_html, zone):
    """表から zone（中東 など）を含む行の金額（カンマ入り数字）を返す。"""
    for row in re.findall(r"<tr.*?</tr>", table_html, re.S):
        cells = re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row, re.S)
        if len(cells) < 2:
            continue
        label = re.sub(r"<[^>]+>|\s", "", cells[0])
        # 「（ハワイ除く）」「（韓国を除く）」の注記に部分一致すると
        # 別の行の額を返してしまう（査読指摘・実ページで確認）
        label = re.sub(r"（[^）]*除く）", "", label)
        if zone in label:
            amt = re.sub(r"<[^>]+>|\s", "", cells[1])
            if re.fullmatch(r"[0-9,]+", amt):
                return amt
    return ""


async def get_fuel_surcharge(session, args, ctx=None) -> str:
    dest = str((args or {}).get("destination") or "").strip()
    zone = ""
    for z, aliases in FUEL_ZONES:
        if any(a in dest for a in aliases):
            zone = z
            break
    if dest and not zone:
        return ("%sの区分は分かりませんでした。中東、欧州、北米、ハワイ、"
                "東アジアなどの方面で聞いてください。" % dest)
    zone = zone or "中東"
    now = time.monotonic()
    html = ""
    if _fuel_cache and now - _fuel_cache[0][0] < FUEL_CACHE_TTL:
        html = _fuel_cache[0][1]
    else:
        try:
            tmo = aiohttp.ClientTimeout(total=15)
            async with session.get(FUEL_URL, timeout=tmo,
                                   headers={"User-Agent": FUEL_UA}) as r:
                html = await r.text()
            if _FUEL_PERIOD_RE.search(html):
                _fuel_cache[:] = [(now, html)]
            else:
                # 形が変わった・メンテ中など。中身が無いものは cache しない
                html = ""
        except Exception as e:
            log.warning("fuel unavailable: %s: %s", type(e).__name__,
                        str(e)[:120])
            html = ""
        if not html and _fuel_cache and now - _fuel_cache[0][0] < FUEL_STALE_MAX:
            html = _fuel_cache[0][1]
    if not html:
        return "error: いま燃油サーチャージを取れませんでした（取得先が応答しません）"
    label, table = _fuel_pick(html, now_jst().date())
    amt = _fuel_amount(table, zone)
    if not amt:
        return "error: いま燃油サーチャージを取れませんでした（ページの形が変わったようです）"
    # 読点は合成の破綻防止（句読点間 34 モーラ以下に保つ）
    return ("いま買う国際線のきっぷだと、ANAの場合、日本から%s方面の"
            "燃油サーチャージは、片道%s円です。この額は、%sです。"
            % (zone, amt, label))


# ---- 渡航情報（外務省 海外安全情報オープンデータ） ---------------------------
# 公式の open data（e-Gov データカタログ登録・キー不要・登録不要）。案内ページ
# anzen.mofa.go.jp/opendata/opendata.html は 2026-08-02 時点でメンテ中だが、
# 配布本体の ezairyu.mofa.go.jp は生きている（実取得で確認）。
# 国別 XML は Light 版でも 1.5MB あるが、危険情報と広域情報は先頭にあり、後ろは
# 領事メールが延々と続くだけなので <mail が出たら読むのをやめる（RPi5 に大きな
# 本文を持たない）。キャッシュは XML でなく読み終えた要約だけ持つ。
TRAVEL_URL = os.environ.get(
    "TRAVEL_URL", "https://www.ezairyu.mofa.go.jp/opendata/country/%sL.xml")
TRAVEL_CACHE_TTL = float(os.environ.get("TRAVEL_CACHE_TTL", "1800"))    # 30 分
TRAVEL_STALE_MAX = float(os.environ.get("TRAVEL_STALE_MAX", "604800"))  # 7 日
TRAVEL_MAX_BYTES = int(os.environ.get("TRAVEL_MAX_BYTES", "400000"))
# 広域情報は複数の国に同じものが出るので、毎回付けると回答が長くなる
TRAVEL_SPOT_DAYS = int(os.environ.get("TRAVEL_SPOT_DAYS", "7"))
# 行き先を言われなかった時。ドバイ経由の旅行が user の主用途
TRAVEL_DEFAULT = os.environ.get("TRAVEL_DEFAULT", "0971")
TRAVEL_MAX_CHARS = 150
TRAVEL_TITLE_MAX = 20

TRAVEL_LEVELS = {4: "退避してください", 3: "渡航は止めてください",
                 2: "不要不急の渡航は止めてください", 1: "十分注意してください"}

# 国コード一覧（外務省 海外安全情報オープンデータの country.xlsx・207 件）
# 形は「電話の国番号:日本語名」。名前の ／ は別名、（）は地域の内訳
TRAVEL_COUNTRY_SRC = (
    "0060:マレーシア 0062:インドネシア 0063:フィリピン 0065:シンガポール 0066:タイ "
    "0082:大韓民国／韓国 0084:ベトナム 0086:中華人民共和国／中国 0091:インド 0092:パキスタン "
    "0094:スリランカ 0095:ミャンマー 0670:東ティモール 0673:ブルネイ 0850:北朝鮮 0852:香港 "
    "0853:マカオ 0855:カンボジア 0856:ラオス 0880:バングラデシュ 0886:台湾 0960:モルディブ "
    "0975:ブータン 0976:モンゴル 0977:ネパール 0061:オーストラリア／豪州 0064:ニュージーランド "
    "0674:ナウル 0675:パプアニューギニア 0676:トンガ 0677:ソロモン諸島 0678:バヌアツ "
    "0679:フィジー 0680:パラオ 0682:クック諸島 0683:ニウエ 0685:サモア独立国 0686:キリバス "
    "0687:ニューカレドニア（仏領） 0688:ツバル 0691:ミクロネシア 0692:マーシャル諸島 "
    "1001:アメリカ合衆国／米国（北マリアナ諸島） 1002:アメリカ合衆国／米国（グアム） 1684:サモア（米領） "
    "9689:タヒチ（仏領ポリネシア） 1000:アメリカ合衆国／米国（本土） 1808:アメリカ合衆国／米国（ハワイ） "
    "9001:カナダ 0051:ペルー 0052:メキシコ 0053:キューバ 0054:アルゼンチン 0055:ブラジル "
    "0056:チリ 0057:コロンビア 0058:ベネズエラ 0473:グレナダ 0501:ベリーズ 0502:グアテマラ "
    "0503:エルサルバドル 0504:ホンジュラス 0505:ニカラグア 0506:コスタリカ 0507:パナマ "
    "0509:ハイチ 0591:ボリビア 0592:ガイアナ 0593:エクアドル 0595:パラグアイ 0597:スリナム "
    "0598:ウルグアイ 0758:セントルシア 0767:ドミニカ国 0784:セントビンセント及びグレナディーン諸島 "
    "0809:ドミニカ共和国 0868:トリニダード・トバゴ 0869:セントクリストファー・ネービス 0876:ジャマイカ "
    "1242:バハマ 1246:バルバドス 1268:アンティグア・バーブーダ 0007:カザフスタン 0030:ギリシャ "
    "0031:オランダ 0032:ベルギー 0033:フランス 0034:スペイン 0036:ハンガリー 0039:イタリア "
    "0040:ルーマニア 0041:スイス 0043:オーストリア "
    "0044:英国／イギリス／グレートブリテン及び北部アイルランド連合王国 0045:デンマーク 0046:スウェーデン "
    "0047:ノルウェー 0048:ポーランド 0049:ドイツ 0351:ポルトガル 0352:ルクセンブルク "
    "0353:アイルランド 0354:アイスランド 0355:アルバニア 0356:マルタ 0357:キプロス／サイプラス "
    "0358:フィンランド 0359:ブルガリア 0370:リトアニア 0371:ラトビア 0372:エストニア "
    "0373:モルドバ 0374:アルメニア 0375:ベラルーシ 0376:アンドラ 0377:モナコ "
    "0378:サンマリノ 0380:ウクライナ 0381:セルビア 0382:モンテネグロ 0385:クロアチア "
    "0386:スロベニア 0387:ボスニア・ヘルツェゴビナ 0389:北マケドニア共和国 0420:チェコ "
    "0421:スロバキア 0423:リヒテンシュタイン 0992:タジキスタン 0993:トルクメニスタン "
    "0994:アゼルバイジャン 0995:ジョージア（旧グルジア） 0996:キルギス 0998:ウズベキスタン "
    "9007:ロシア 9039:バチカン市国 9381:コソボ 0090:トルコ 0093:アフガニスタン 0098:イラン "
    "0961:レバノン 0962:ヨルダン 0963:シリア 0964:イラク 0965:クウェート "
    "0966:サウジアラビア 0967:イエメン 0968:オマーン 0970:パレスチナ 0971:アラブ首長国連邦 "
    "0972:イスラエル 0973:バーレーン 0974:カタール 0020:エジプト 0027:南アフリカ共和国 "
    "0211:南スーダン 0212:モロッコ 0213:アルジェリア 0216:チュニジア 0218:リビア "
    "0220:ガンビア 0221:セネガル 0222:モーリタニア 0223:マリ 0224:ギニア "
    "0225:コートジボワール 0226:ブルキナファソ 0227:ニジェール 0228:トーゴ 0229:ベナン "
    "0230:モーリシャス 0231:リベリア 0232:シエラレオネ 0233:ガーナ 0234:ナイジェリア "
    "0235:チャド 0236:中央アフリカ 0237:カメルーン 0238:カーボベルデ 0239:サントメ・プリンシペ "
    "0240:赤道ギニア 0241:ガボン 0242:コンゴ共和国 0243:コンゴ民主共和国 0244:アンゴラ "
    "0245:ギニアビサウ 0248:セーシェル 0249:スーダン 0250:ルワンダ 0251:エチオピア "
    "0252:ソマリア 0253:ジブチ 0254:ケニア 0255:タンザニア 0256:ウガンダ 0257:ブルンジ "
    "0258:モザンビーク 0260:ザンビア 0261:マダガスカル 0263:ジンバブエ 0264:ナミビア "
    "0265:マラウイ 0266:レソト 0267:ボツワナ 0268:エスワティニ 0269:コモロ 0291:エリトリア "
    "9212:西サハラ"
)

# 読み上げるときの呼び方（表の名前が長い・括弧付きで割れているものだけ）
TRAVEL_DISPLAY = {
    "1000": "アメリカ", "1001": "北マリアナ諸島", "1002": "グアム",
    "1808": "ハワイ", "1684": "アメリカ領サモア", "9689": "タヒチ",
    "0687": "ニューカレドニア", "0044": "イギリス", "0086": "中国",
    "0082": "韓国", "0061": "オーストラリア", "0995": "ジョージア",
    "0389": "北マケドニア",
}

# 表に無い言い方（都市名・通称）。表の名前より先に見る＝「米国」のように
# 同じ言い方が本土・グアム・ハワイに並ぶとき、本土を選ばせるため
TRAVEL_ALIASES = {
    "0971": ("ドバイ", "アブダビ", "UAE"), "0974": ("ドーハ",),
    "0966": ("リヤド", "ジェッダ"), "0090": ("イスタンブール", "トルコ共和国"),
    "0098": ("テヘラン",), "0020": ("カイロ",), "0995": ("グルジア",),
    "0082": ("ソウル", "釜山"), "0086": ("北京", "上海"), "0886": ("台北",),
    "0852": ("香港",), "0066": ("バンコク",), "0084": ("ハノイ", "ホーチミン"),
    "0063": ("マニラ", "セブ"), "0060": ("クアラルンプール",),
    "0062": ("バリ", "ジャカルタ"), "0091": ("デリー", "ムンバイ"),
    "0061": ("シドニー", "メルボルン"), "0064": ("オークランド",),
    "1808": ("ホノルル",), "1001": ("サイパン",),
    "1000": ("アメリカ", "アメリカ合衆国", "米国", "USA", "ニューヨーク",
             "ロサンゼルス", "サンフランシスコ", "ラスベガス"),
    "9001": ("バンクーバー", "トロント"), "0033": ("パリ",),
    "0044": ("ロンドン",), "0039": ("ローマ", "ミラノ", "ベネチア"),
    "0034": ("バルセロナ", "マドリード"),
    "0049": ("ベルリン", "ミュンヘン", "フランクフルト"),
    "0043": ("ウィーン",), "0041": ("チューリッヒ", "ジュネーブ"),
    "0031": ("アムステルダム",), "0358": ("ヘルシンキ",),
    "9007": ("モスクワ",), "0972": ("エルサレム", "テルアビブ"),
    "1684": ("アメリカ領サモア", "米領サモア"), "0685": ("サモア",),
    "0767": ("ドミニカ",),
}


def _travel_index():
    """表の名前から「引き当てに使う言い方」を作る。"""
    drop = {"本土"}   # 「アメリカ合衆国（本土）」の括弧内は誤爆するので使わない
    idx, names = [], {}
    for item in TRAVEL_COUNTRY_SRC.split():
        cd, _, name = item.partition(":")
        names[cd] = name
        aliases = []
        for part in name.split("／"):
            m = re.match(r"^(.*)（(.+)）$", part)
            if m:
                aliases.append(m.group(1))
                aliases.append(m.group(2))
            else:
                aliases.append(part)
        idx.append((cd, tuple(a for a in aliases if a and a not in drop)))
    return idx, names


TRAVEL_INDEX, TRAVEL_NAMES = _travel_index()
TRAVEL_ALIAS_INDEX = list(TRAVEL_ALIASES.items())
_travel_cache = {}   # 国コード -> (取得時刻 monotonic, 読み終えた要約)


def _travel_find(q: str) -> str:
    """言われた文字列から国コードを引く。

    「いちばん長く一致した国」を選ぶ。短い名前が長い名前に含まれる組
    （インド / インドネシア、ギニア / パプアニューギニア、ドミニカ国 /
    ドミニカ共和国）があるので、最長一致でないと取り違える。同じ長さで
    並んだ時だけこちらの指定を勝たせる（「米国」は本土・グアム・ハワイの
    3 つに並ぶので、表の順ではなく本土を選ばせたい）。
    """
    q = re.sub(r"[\s　]+", "", q or "")
    if not q:
        return TRAVEL_DEFAULT
    best_cd, best_key = "", (0, 0)
    for table, mine in ((TRAVEL_INDEX, 0), (TRAVEL_ALIAS_INDEX, 1)):
        for cd, aliases in table:
            for a in aliases:
                if (len(a), mine) > best_key and a in q:
                    best_cd, best_key = cd, (len(a), mine)
    if best_cd:
        return best_cd
    # 言われた方が正式名称より短い場合（「南アフリカ」に対し表は
    # 「南アフリカ共和国」）。ただし「コンゴ」のように複数の国に当たる
    # 言い方は、取り違えると危ないので選ばずに聞き返す
    if len(q) >= 3:
        hit = set()
        for table, _mine in ((TRAVEL_INDEX, 0), (TRAVEL_ALIAS_INDEX, 1)):
            for cd, aliases in table:
                if any(q in a for a in aliases):
                    hit.add(cd)
        if len(hit) == 1:
            return hit.pop()
    return ""


def _travel_name(cd: str) -> str:
    if cd in TRAVEL_DISPLAY:
        return TRAVEL_DISPLAY[cd]
    return re.sub(r"（.*?）", "", TRAVEL_NAMES.get(cd, "").split("／")[0])


def _travel_ymd(s):
    m = re.match(r"(\d{4})/(\d{1,2})/(\d{1,2})", (s or "").strip())
    if not m:
        return None
    try:
        return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _travel_parse(xml: str) -> dict:
    """危険情報のレベル・発表日・広域の注意喚起を読み出す。

    形が違うものは読まずに投げる。取得先が 200 でメンテ用の HTML を返した
    とき、素通りさせると「危険情報は出ていません」と断言してしまう
    （レベル 4 の国で「安全」と読み上げるのが最悪の壊れ方）。
    """
    if "<opendata" not in xml or "<riskLevel" not in xml:
        raise ValueError("危険情報の形をしていない応答")

    def one(tag):
        m = re.search("<" + tag + r"[^>]*>(.*?)</" + tag + ">", xml, re.S)
        return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""

    spots, seen = [], set()
    for m in re.finditer(r"<wideareaSpot>(.*?)</wideareaSpot>", xml, re.S):
        body = m.group(1)
        t = re.search(r"<title>(.*?)</title>", body, re.S)
        if not t:
            continue
        title = re.sub(r"\s+", "", t.group(1))
        if not title or title in seen:
            continue
        seen.add(title)
        d = re.search(r"<leaveDate[^>]*>(.*?)</leaveDate>", body, re.S)
        spots.append((_travel_ymd(d.group(1)) if d else None, title))
    return {
        "risk": [n for n in (4, 3, 2, 1) if one("riskLevel%d" % n) == "1"],
        "infection": [n for n in (4, 3, 2, 1)
                      if one("infectionLevel%d" % n) == "1"],
        "date": _travel_ymd(one("riskLeaveDate")),
        "title": one("riskTitle"),
        "spots": spots,
    }


async def _travel_fetch(session, cd: str) -> str:
    """必要な先頭だけ読んで打ち切る（全部で 1.5MB あるため）。"""
    buf = bytearray()
    tmo = aiohttp.ClientTimeout(total=30)
    async with session.get(TRAVEL_URL % cd, timeout=tmo,
                           headers={"User-Agent": FUEL_UA}) as r:
        r.raise_for_status()
        async for chunk in r.content.iter_chunked(65536):
            buf += chunk
            if len(buf) >= TRAVEL_MAX_BYTES:
                break
            # 領事メールが始まったら以降は要らない。境目で切れても、
            # 途中の <wideareaSpot> は閉じ tag が無いので拾われない
            if buf.find(b"<mail>") >= 0 or buf.find(b"<mail ") >= 0:
                break
    return buf.decode("utf-8", "ignore")


def _travel_spot_title(title: str) -> str:
    """広域情報の題名を読み上げ向けに詰める。

    実物は「【広域情報】海外における写真・動画撮影及びＳＮＳ等への投稿に
    関する注意喚起」のように読点が無いまま 37 字あり、そのまま入れると
    読点で区切った 1 かたまりが 40 字を超えて合成が崩れる。
    """
    title = re.sub(r"^【[^】]*】", "", title).strip()
    if len(title) <= TRAVEL_TITLE_MAX:
        return title
    head = title[:TRAVEL_TITLE_MAX]
    cut = max(head.rfind("・"), head.rfind("（"), head.rfind("、"))
    if cut >= TRAVEL_TITLE_MAX // 2:
        head = head[:cut]
    head = re.sub(r"[のへにをとがはや、・]+$", "", head)
    return head + "など"


def _travel_say(cd: str, data: dict, today) -> str:
    name = _travel_name(cd)
    if data["risk"]:
        top = data["risk"][0]
        many = "いちばん高いところで" if len(data["risk"]) > 1 else ""
        head = ("%sの危険情報は、%sレベル%d、%s、の段階です。"
                % (name, many, top, TRAVEL_LEVELS[top]))
    else:
        head = "%sに危険情報は出ていません。" % name
    # (つなぐ時の形, 文を終える時の形)。「発表です、最近では」を避ける
    tail = []
    if data["risk"] and data["date"]:
        d = "%d年%d月%d日" % (data["date"].year, data["date"].month,
                              data["date"].day)
        tail.append(("これは%sの発表で" % d, "これは%sの発表です" % d))
    if data["infection"]:
        n = data["infection"][0]
        tail.append(("感染症の危険情報もレベル%dで" % n,
                     "感染症の危険情報もレベル%dです" % n))
    recent = None
    for d, title in data["spots"]:
        if d and (today - d).days <= TRAVEL_SPOT_DAYS:
            if recent is None or d > recent[0]:
                recent = (d, title)
    if recent:
        # 広域情報は複数の国にまたがって出るもの。その国だけの話に
        # 聞こえないよう「広い地域向け」と断る
        title = _travel_spot_title(recent[1])
        tail.append(("広い地域向けの注意喚起として、%sも出ていますが" % title,
                     "広い地域向けの注意喚起として、%sも出ています" % title))
    kept = []
    for clause in tail:
        cand = [c[0] for c in kept] + [clause[1]]
        if len(head) + len("、".join(cand)) + 1 > TRAVEL_MAX_CHARS:
            break
        kept.append(clause)
    if not kept:
        return head
    return head + "、".join([c[0] for c in kept[:-1]] + [kept[-1][1]]) + "。"


async def get_travel_advisory(session, args, ctx=None) -> str:
    q = str((args or {}).get("country") or "").strip()
    cd = _travel_find(q)
    if not cd:
        return ("%sの渡航情報は分かりませんでした。国の名前で聞いてください。"
                % q[:16])
    now = time.monotonic()
    got = _travel_cache.get(cd)
    data = got[1] if got and now - got[0] < TRAVEL_CACHE_TTL else None
    if data is None:
        try:
            data = _travel_parse(await _travel_fetch(session, cd))
            _travel_cache[cd] = (now, data)
        except Exception as e:
            log.warning("travel unavailable: %s: %s", type(e).__name__,
                        str(e)[:120])
            data = got[1] if got and now - got[0] < TRAVEL_STALE_MAX else None
    if data is None:
        return "error: いま渡航情報を取れませんでした（取得先が応答しません）"
    return _travel_say(cd, data, now_jst().date())


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
}, {
    "type": "function",
    "function": {
        "name": "get_usdjpy",
        "description": "いまのドル円（USD/JPY）の為替レートを調べる。"
                       "「ドル円いくら？」「円安どうなってる？」など為替の質問に使う。",
        "parameters": {"type": "object", "properties": {}},
    },
}, {
    "type": "function",
    "function": {
        "name": "get_stock_index",
        "description": "株価指数を調べる。日経平均（日本株）・ダウ平均・S&P500（米国株）。"
                       "「日経平均いくら？」「アメリカの株どう？」「株価教えて」など"
                       "株の質問に使う。指定が無ければ 3 指数まとめて答える。",
        "parameters": {
            "type": "object",
            "properties": {
                "index": {"type": "string",
                          "enum": ["nikkei", "dow", "sp500", "nasdaq"],
                          "description": "nikkei=日経平均、dow=ダウ平均、sp500=S&P500、nasdaq=ナスダック総合指数。"
                                         "省略すると全部"},
            },
        },
    },
}, {
    "type": "function",
    "function": {
        "name": "get_llm_quota",
        "description": "さくらのAI Engine の無料枠（月3000リクエスト）をどれだけ使ったか・"
                       "残りが何回かを答える。「無料枠あとどれくらい？」「さくらの残りは？」"
                       "「API あと何回使える？」など利用量の質問に使う。",
        "parameters": {"type": "object", "properties": {}},
    },
}, {
    "type": "function",
    "function": {
        "name": "get_crypto",
        "description": "ビットコイン（BTC）とイーサリアム（ETH）のいまの価格を日本円で調べる。"
                       "「ビットコインいくら？」「イーサリアムの値段は？」「仮想通貨どう？」"
                       "など暗号資産の質問に使う。指定が無ければ両方まとめて答える。",
        "parameters": {
            "type": "object",
            "properties": {
                "coin": {"type": "string",
                         "enum": ["btc", "eth"],
                         "description": "btc=ビットコイン、eth=イーサリアム。省略すると両方"},
            },
        },
    },
}, {
    "type": "function",
    "function": {
        "name": "get_news",
        "description": "きょうの主なニュースの見出しを調べる。「ニュース教えて」"
                       "「何かニュースあった？」などに使う。",
        "parameters": {"type": "object", "properties": {}},
    },
}, {
    "type": "function",
    "function": {
        "name": "get_quake",
        "description": "最近あった地震（いつ・どこ・マグニチュード・最大震度）を調べる。"
                       "「さっき地震あった？」「揺れたのどこ？」「地震どうなってる？」"
                       "などに使う。",
        "parameters": {"type": "object", "properties": {}},
    },
}, {
    "type": "function",
    "function": {
        "name": "get_warning",
        "description": "気象の警報・注意報が出ているか調べる（対象は神奈川県）。"
                       "「警報出てる？」「大雨警報は？」「注意報ある？」などに使う。",
        "parameters": {"type": "object", "properties": {}},
    },
}, {
    "type": "function",
    "function": {
        "name": "get_typhoon",
        "description": "台風が発生しているか・いまどこにいるか・勢力を調べる。"
                       "「台風来てる？」「台風どこにいる？」「台風大丈夫？」"
                       "などに使う。",
        "parameters": {"type": "object", "properties": {}},
    },
}, {
    "type": "function",
    "function": {
        "name": "get_heat",
        "description": "熱中症の危険度（暑さ指数 WBGT）と熱中症警戒アラートを調べる。"
                       "「今日暑い？」「熱中症大丈夫？」「外出ていい？」"
                       "「熱中症アラート出てる？」などに使う。",
        "parameters": {"type": "object", "properties": {}},
    },
}, {
    "type": "function",
    "function": {
        "name": "get_onthisday",
        "description": "きょうが何の日か・過去のきょう何があったかを調べる。"
                       "「今日は何の日？」「きょう何かあった日？」"
                       "「歴史で今日は？」などに使う。",
        "parameters": {"type": "object", "properties": {}},
    },
}, {
    "type": "function",
    "function": {
        "name": "get_sky",
        "description": "月齢（今夜の月の形）と、日の出・日の入りの時刻を調べる。"
                       "「今日の月は？」「満月いつ？」「日の入り何時？」"
                       "「あと何時間明るい？」などに使う。",
        "parameters": {"type": "object", "properties": {}},
    },
}, {
    "type": "function",
    "function": {
        "name": "get_train",
        "description": "電車が遅れているか・止まっていないかを調べる。"
                       "「電車遅れてる？」「京急動いてる？」「電車大丈夫？」"
                       "などに使う。",
        "parameters": {"type": "object", "properties": {}},
    },
}, {
    "type": "function",
    "function": {
        "name": "get_fuel_surcharge",
        "description": "国際線の燃油サーチャージ（燃油特別付加運賃）を調べる。"
                       "「ドバイまでのサーチャージいくら？」「燃油サーチャージは？」"
                       "などに使う。行き先を言われなければ中東（ドバイ）方面。",
        "parameters": {
            "type": "object",
            "properties": {
                "destination": {"type": "string",
                                "description": "行き先。例: ドバイ、パリ、ハワイ。省略可"},
            },
        },
    },
}, {
    "type": "function",
    "function": {
        "name": "get_travel_advisory",
        "description": "海外の渡航情報（外務省の危険情報・感染症危険情報・"
                       "注意喚起）を調べる。「ドバイは安全？」"
                       "「タイの渡航情報は？」「フランス行っても大丈夫？」"
                       "などに使う。国を言われなければアラブ首長国連邦。",
        "parameters": {
            "type": "object",
            "properties": {
                "country": {"type": "string",
                            "description": "国や都市の名前。例: ドバイ、タイ、"
                                           "フランス。省略可"},
            },
        },
    },
}]

HANDLERS = {"get_weather": get_weather, "get_usdjpy": get_usdjpy,
            "get_stock_index": get_stock_index,
            "get_llm_quota": get_llm_quota,
            "get_crypto": get_crypto,
            "get_news": get_news,
            "get_quake": get_quake,
            "get_warning": get_warning,
            "get_typhoon": get_typhoon,
            "get_heat": get_heat,
            "get_onthisday": get_onthisday,
            "get_sky": get_sky,
            "get_train": get_train,
            "get_fuel_surcharge": get_fuel_surcharge,
            "get_travel_advisory": get_travel_advisory}


def specs():
    return SPECS


def has(name: str) -> bool:
    return name in HANDLERS


async def call(session, name: str, args: dict, ctx=None) -> str:
    """ctx = 呼び出しの文脈（発話・直前に調べた日）。無くても動く。"""
    return await HANDLERS[name](session, args or {}, ctx)
