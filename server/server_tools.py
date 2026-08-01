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
import os
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
                          "enum": ["nikkei", "dow", "sp500"],
                          "description": "nikkei=日経平均、dow=ダウ平均、sp500=S&P500。"
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
}]

HANDLERS = {"get_weather": get_weather, "get_usdjpy": get_usdjpy,
            "get_stock_index": get_stock_index,
            "get_llm_quota": get_llm_quota,
            "get_crypto": get_crypto,
            "get_news": get_news,
            "get_quake": get_quake,
            "get_warning": get_warning}


def specs():
    return SPECS


def has(name: str) -> bool:
    return name in HANDLERS


async def call(session, name: str, args: dict, ctx=None) -> str:
    """ctx = 呼び出しの文脈（発話・直前に調べた日）。無くても動く。"""
    return await HANDLERS[name](session, args or {}, ctx)
