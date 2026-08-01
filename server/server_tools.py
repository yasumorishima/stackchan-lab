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
}]

HANDLERS = {"get_weather": get_weather, "get_usdjpy": get_usdjpy}


def specs():
    return SPECS


def has(name: str) -> bool:
    return name in HANDLERS


async def call(session, name: str, args: dict, ctx=None) -> str:
    """ctx = 呼び出しの文脈（発話・直前に調べた日）。無くても動く。"""
    return await HANDLERS[name](session, args or {}, ctx)
