"""本番ログの返事を、いまの版の「文の切り方」に流し直して比べる（LLM も本体も要らない）。

本番で LLM が書いた本文（ログの「LLM: 」）を、いまの SentenceFeed に小分けで流し、
本番で実際に読み上げた文（sentence_start）と並べる。さくらには投げないので無償枠を使わない。
同じ入力を何度でも流せるので、直した前後の違いだけが出る
（Anthropic「Migrating our Messages API to Rust」2026-11-25 の、本番を再生して差を見るやり方）。

ログは非公開レポ stackchan-logs にある。中身（会話）はこのレポに入れない。

  ./.venv/bin/python replay_log.py ../../stackchan-logs/logs/2026-11-28.log [...]
  -v で同じだった回も出す

見るもの:
  * 違い: 本番で読んだ文の列と、いまの版で読む文の列が違う回
  * 読めない文: いまの版でも文字化け（U+FFFD）や制御文字が読み上げに渡る回（0 であるべき）
つなぎの言葉・豆知識は LLM の本文ではないので本番側の列から除く。歌の回は比べない。
"""
import ast
import asyncio
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app

SAID_RE = re.compile(r'"state": "sentence_start", "text": "(.*)"\}$')
BAD_RE = re.compile("[�\x00-\x08\x0b-\x1f\x7f]")


def _not_llm(s: str) -> bool:
    """つなぎ・豆知識・歌など、LLM の本文から来ていない読み上げ。"""
    extra = list(getattr(app, "THINK_FILLERS", [])) + list(getattr(app, "TRIVIA", []))
    return s in extra or s.startswith("お待たせ") or s.endswith("の応援歌")


def turns(path):
    """(聞き取った言葉, 本番で読んだ文の列, LLM の本文, 途中で止めたか) を順に返す。

    本文は「LLM 生: 」（改行込み・2026-11-28 から）があればそれ、無ければ「LLM: 」
    （改行が空白になっているので、表や箇条書きの区切りは再現できない）。
    歌の回は本文を流さないので除く。
    """
    heard, said, cut, song, pending = None, [], False, False, None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            if " INFO LLM 生: " in line and pending:
                try:
                    pending = (pending[0], pending[1],
                               ast.literal_eval(line.split(" INFO LLM 生: ", 1)[1]),
                               pending[3], pending[4])
                except (ValueError, SyntaxError):
                    pass
                continue
            if pending:
                # 本番で何も読まなかった回（古いログの歌の回など）は比べようがない
                if not pending[4] and pending[1]:
                    yield pending[:4]
                pending = None
            if " INFO STT: " in line:
                heard, said, cut, song = line.split(" INFO STT: ", 1)[1], [], False, False
                continue
            if heard is None:
                continue
            if "tool sing_cheer_song(" in line and "-> ok" in line:
                song = True
            if "残りをやめる" in line:
                cut = True
            m = SAID_RE.search(line)
            if m:
                s = m.group(1)
                # 2 片に分けた文は同じ文の札をもう一度送る＝同じ文として 1 回に数える
                if not _not_llm(s) and (not said or said[-1] != s):
                    said.append(s)
                continue
            if " INFO LLM: " in line:
                pending = (heard, said, line.split(" INFO LLM: ", 1)[1], cut, song)
                heard = None
    if pending and not pending[4] and pending[1]:
        yield pending[:4]


async def replay(text, chunk=5):
    q = asyncio.Queue()
    feed = app.SentenceFeed(q)
    for i in range(0, len(text), chunk):
        await feed.add(text[i:i + chunk])
    await feed.close()
    return [s async for s in app._aiter_queue(q)]


def main(argv):
    verbose = "-v" in argv
    paths = [a for a in argv if a != "-v"]
    if not paths:
        print(__doc__)
        return 2
    n = same = bad = 0
    for path in paths:
        for heard, said, text, cut in turns(path):
            n += 1
            now = asyncio.run(replay(text))
            broken = [s for s in now if BAD_RE.search(s)]
            bad += bool(broken)
            # 話しかけられて途中でやめた回は、読んだところまでが同じなら同じ
            if (now == said or (cut and now[:len(said)] == said)) and not broken:
                same += 1
                if verbose:
                    print("同じ %s: %s" % (heard, " / ".join(now)))
                continue
            print("%s %s（%s）" % ("読めない" if broken else "違い",
                                   heard, os.path.basename(path)))
            print("  本番: %s" % " / ".join(said))
            print("  いま: %s" % " / ".join(now))
    print("")
    print("%d 回中 同じ %d・違い %d・読めない文 %d" % (n, same, n - same, bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
