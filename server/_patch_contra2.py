# -*- coding: utf-8 -*-
"""歌を用意できている間は「見つからない・歌えない」を読み上げない。

user 実機（2026-08-08 22:00）: 柴田の歌を 10.8 秒鳴らした直後に「ごめんね、
柴田選手の応援歌はデータに見つからなかったよ」。実ログでは歌の用意に成功した
あと、選手名を空で歌詞の道具を呼び、その一覧を「無い」と読んで否定していた。
**言っていることと鳴っている音が食い違う**のがいちばん悪い。

読み上げ済みの文は言い直せないので、**読む前に差し替える**。
"""
import io
import sys

P = "/home/yasu/stackchan-server/app.py"
s = io.open(P, encoding="utf-8").read()
if "NEGATIVE_SONG_RE" in s:
    sys.exit("すでに適用済み")

old = 'SING_ASK_RE = re.compile(r"(.{1,12}?)\s*の\s*(?:応援歌|おうえんか|応歌)")'
new = '''SING_ASK_RE = re.compile(r"(.{1,12}?)\s*の\s*(?:応援歌|おうえんか|応歌)")
# 歌を用意できているのに「歌えない」と言う矛盾（実機 2026-08-08 22:00）
NEGATIVE_SONG_RE = re.compile(r"見つから|歌えな|ありません|見つけられ")'''
if s.count(old) != 1:
    sys.exit("SING_ASK_RE の目印が 1 つでない (%d)" % s.count(old))
s = s.replace(old, new, 1)

old2 = '''            first = await queue.get()
            if first is not None:'''
new2 = '''            first = await queue.get()

            async def _no_deny(src):
                """歌を用意できている間は否定的な文を読まない。"""
                async for line in src:
                    if state.get("song") and NEGATIVE_SONG_RE.search(line or ""):
                        log.info("歌は用意できているので否定の文は読まない: %r",
                                 line)
                        continue
                    yield line

            if first is not None and state.get("song") and \
                    NEGATIVE_SONG_RE.search(first):
                log.info("歌は用意できているので最初の文を差し替えた: %r", first)
                first = fix_reading("%sの応援歌を歌うね。"
                                    % state["song"]["name"])
            if first is not None:'''
if s.count(old2) != 1:
    sys.exit("streaming の目印が 1 つでない (%d)" % s.count(old2))
s = s.replace(old2, new2)

old3 = '''                    await speak(source=_aiter_queue(queue, first))'''
new3 = '''                    await speak(source=_no_deny(_aiter_queue(queue, first)))'''
if s.count(old3) != 1:
    sys.exit("speak の目印が 1 つでない (%d)" % s.count(old3))
s = s.replace(old3, new3)

old4 = '''                if queued and reply.startswith("うまく答えられませんでした"):'''
new4 = '''                if queued and (reply.startswith("うまく答えられませんでした")
                               or NEGATIVE_SONG_RE.search(reply or "")):'''
if s.count(old4) != 1:
    sys.exit("差し替えの目印が 1 つでない (%d)" % s.count(old4))
s = s.replace(old4, new4)
io.open(P, "w", encoding="utf-8").write(s)
print("patched app.py")
