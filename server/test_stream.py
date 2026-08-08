"""流しながら読み上げる仕組みの試験（LLM も本体も要らない）。

見るのは 2 つ:
  1. SentenceFeed が、全部そろってから shorten_reply で削るのと同じ形に
     なること（文の数・字数・言い直しの畳み・最初のかたまりの分割）
  2. 流れてくる文をそのまま読み上げに渡す道（_aiter_queue）が、
     終わり（None）で止まること

  ./.venv/bin/python test_stream.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app

ok = ng = 0


def check(name, cond, got=""):
    global ok, ng
    if cond:
        ok += 1
        print("OK %s %s" % (name, got))
    else:
        ng += 1
        print("NG %s %s" % (name, got))


async def drain(text, chunk=7, verbatim=False):
    """本文を小分けに流して、読み上げに渡る文の列を得る。"""
    q = asyncio.Queue()
    feed = app.SentenceFeed(q)
    feed.verbatim = verbatim
    for i in range(0, len(text), chunk):
        await feed.add(text[i:i + chunk])
    await feed.close()
    out = []
    async for s in app._aiter_queue(q):
        out.append(s)
    return out


async def main():
    # 1) 一括で削った時と同じ中身になる
    for text, memo in [
            ("はい。今日の横浜は晴れです。気温は30度。", "ふつうの返事"),
            ("横浜は晴れです。" * 20, "長すぎる返事"),
            ("うん。うん。うん。そうだね。", "言い直しがある返事"),
    ]:
        got = "".join(await drain(text))
        want = app.shorten_reply(app.clean_reply(text))
        check("一括と同じ形になる（%s）" % memo, got == want,
              "流し %r / 一括 %r" % (got[:40], want[:40]))

    # 2) 応援歌は繰り返しを畳まない
    song = "オオオオー。オオオオオ。かっとばせ。かっとばせー！"
    got = "".join(await drain(song, verbatim=True))
    check("応援歌は繰り返しを残す", got == app.shorten_reply(app.clean_reply(song), True),
          repr(got[:40]))

    # 3) 最初のかたまりは読点で割って先に鳴らす
    parts = await drain("わかりました、今日の横浜の天気をお伝えしますね。おわり。")
    check("最初のかたまりを短く割る", len(parts) >= 3 and parts[0].endswith("、"),
          "%d 片 先頭=%r" % (len(parts), parts[0]))

    # 4) 文の終わりが無いまま終わった時は、丸ごと捨てずに読む
    parts = await drain("横浜は晴れ")
    check("言いかけでも 1 文も無ければ読む", parts == ["横浜は晴れ"], repr(parts))

    # 5) 完結した文があれば、言いかけの末尾は読まない
    parts = await drain("横浜は晴れです。気温は")
    check("完結した文があれば言いかけは落とす",
          "".join(parts) == "横浜は晴れです。", repr(parts))

    # 6) 空の生成でも止まらない（終わりが必ず来る）
    parts = await drain("")
    check("空でも終わる", parts == [], repr(parts))

asyncio.run(main())
print("")
print("%d/%d" % (ok, ok + ng))
