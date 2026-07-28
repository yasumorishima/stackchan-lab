"""地名テーブル(places.py)の生成器。

Open-Meteo の geocoding は日本語名を引けない（「札幌」は 0 件、"Sapporo" なら当たる）。
一方で応答には日本語の市名・都道府県名が入っているので、ローマ字で引いて
返ってきた日本語名と座標をそのまま表に落とす。座標を人手で書かないための道具。
"""
import json
import sys
import time
import urllib.parse
import urllib.request

CITIES = [
    "Sapporo", "Aomori", "Morioka", "Sendai", "Akita", "Yamagata", "Fukushima",
    "Mito", "Utsunomiya", "Maebashi", "Saitama", "Chiba", "Tokyo", "Yokohama",
    "Niigata", "Toyama", "Kanazawa", "Fukui", "Kofu", "Nagano", "Gifu",
    "Shizuoka", "Nagoya", "Tsu", "Otsu", "Kyoto", "Osaka", "Kobe", "Nara-shi",
    "Wakayama", "Tottori", "Matsue", "Okayama", "Hiroshima", "Yamaguchi",
    "Tokushima", "Takamatsu", "Matsuyama", "Kochi", "Fukuoka", "Saga",
    "Nagasaki", "Kumamoto", "Oita", "Miyazaki", "Kagoshima", "Naha",
    "Kawasaki", "Sagamihara", "Hachioji", "Funabashi", "Kitakyushu", "Hamamatsu",
]

SUFFIX = ("都", "道", "府", "県", "市", "区", "町", "村")


def best_jp(results):
    """日本の候補から目的地らしいものを選ぶ。

    先頭が目的地とは限らない。「Tottori」は 1 位が釧路市の一地区（PPL・人口なし）で、
    鳥取市（PPLA・188,465人）は 4 位に出る。行政の中心を表す feature_code と
    人口で並べ替えてから採る。
    """
    jp = [h for h in (results or []) if h.get("country_code") == "JP"]
    if not jp:
        return None
    rank = {"PPLC": 3, "PPLA": 2, "PPLA2": 1}
    jp.sort(key=lambda h: (rank.get(h.get("feature_code"), 0), h.get("population") or 0),
            reverse=True)
    return jp[0]


def fetch(name):
    url = ("https://geocoding-api.open-meteo.com/v1/search?"
           + urllib.parse.urlencode({"name": name, "count": 20,
                                     "language": "ja", "format": "json"}))
    with urllib.request.urlopen(url, timeout=30) as r:
        body = json.loads(r.read())
    return best_jp(body.get("results"))


def variants(word):
    """「横浜市」から「横浜」も引けるように、末尾の行政区分を落とした形も足す。"""
    out = {word}
    w = word
    while len(w) > 2 and w[-1] in SUFFIX:
        w = w[:-1]
        if w:
            out.add(w)
    return out


def main():
    table = {}
    pref_seen = set()
    missing = []
    for city in CITIES:
        hit = fetch(city)
        time.sleep(0.4)
        if hit is None:
            missing.append(city)
            continue
        lat, lon = hit["latitude"], hit["longitude"]
        ja = hit.get("name") or city
        pref = hit.get("admin1") or ""
        keys = variants(ja) | {city.lower()}
        if pref and pref not in pref_seen:
            pref_seen.add(pref)
            keys |= variants(pref)
        for k in sorted(keys):
            table.setdefault(k, (round(lat, 4), round(lon, 4), ja))
    lines = ['"""地名 -> (緯度, 経度, 表示名)。gen_places.py が Open-Meteo geocoding から生成。',
             '',
             '座標を手書きしないための自動生成ファイル。編集せず gen_places.py を回し直す。',
             '"""',
             "PLACES = {"]
    for k in sorted(table):
        lat, lon, ja = table[k]
        lines.append('    "%s": (%s, %s, "%s"),' % (k, lat, lon, ja))
    lines.append("}")
    text = "\n".join(lines) + "\n"
    with open("places.py", "w", encoding="utf-8") as f:
        f.write(text)
    print("entries: %d  missing: %s" % (len(table), missing or "none"))


main()
