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

    # 7) 表は句点が無くても 1 行ずつ読み始める・見出しと区切り行は読まない
    #    （実機 2026-11-14「今日の株価」: 表が全部そろうまで 7 秒黙った）
    table = ("今日の株価はこんな感じだよ：\n| 指数 | 終値 |\n|---|---|\n"
             "| 日経平均 | 61,142円 |\n| ダウ | 52,348ドル |\n")
    q = asyncio.Queue()
    feed = app.SentenceFeed(q)
    first = None
    for i in range(0, len(table), 7):
        await feed.add(table[i:i + 7])
        if first is None and not q.empty():
            first = i
    await feed.close()
    parts = [x async for x in app._aiter_queue(q)]
    check("表は全部そろう前に読み始める", first is not None and first < len(table) - 20,
          repr(first))
    check("表の見出し・区切りは読まない",
          not any("指数" in p or "---" in p or "|" in p for p in parts), repr(parts))
    check("表の最後の行まで読む", any("ダウ" in p for p in parts), repr(parts))
    parts = await drain("- 日経平均: 61,142円\n- ダウ: 52,348ドル")
    check("箇条書きの最後の行も読む", any("ダウ" in p for p in parts), repr(parts))

    # 8) ニュースはモデルに書き直させず、見出しをそのまま読む・もう一度聞かない
    #    （実機 2026-11-28: 「ICC（国際刑事警察）」と言い換えて中身を作った）
    news = "主なニュースです。警察 ICC赤根所長の安全確保。西武 高橋光成 1年契約で合意。以上です。"
    asked = []

    async def fake_chat(session, msgs, tools=None, on_delta=None, **kw):
        asked.append(len(msgs))
        return {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "get_news", "arguments": "{}"}}]}

    async def fake_tool(name, args):
        return news

    real_chat, real_backend = app.sakura_chat, app.LLM_BACKEND
    app.sakura_chat, app.LLM_BACKEND = fake_chat, "sakura"
    try:
        q = asyncio.Queue()
        feed = app.SentenceFeed(q)
        reply, _ = await app.respond(None, [{"role": "user", "content": "ニュース"}],
                                     tools=[], call_tool=fake_tool, feed=feed)
        await feed.close()
        parts = [x async for x in app._aiter_queue(q)]
    finally:
        app.sakura_chat, app.LLM_BACKEND = real_chat, real_backend
    check("ニュースはモデルにもう一度聞かない", len(asked) == 1, repr(asked))
    check("ニュースの見出しをそのまま読む",
          "ICC赤根所長" in "".join(parts) and "国際刑事" not in "".join(parts),
          repr(parts))
    check("見出しごとに文を切る", len(parts) >= 3, repr(parts))

asyncio.run(main())
print("")
print("%d/%d" % (ok, ok + ng))
