import sys

sys.path.insert(0, "/home/yasu/stackchan-server")
import app

CASES = [
    ("</tool_call>", ""),
    ("こんにちは。", "こんにちは。"),
    ('はい<tool_call>{"name": "get_weather", "arguments": {}}', "はい"),
    ('あしたは晴れです。{"name": "x", "arguments": {"a": 1}} おわり',
     "あしたは晴れです。 おわり"),
    ('途中で切れた {"name": "get_weather"', "途中で切れた"),
    ("", ""),
    ("21時15分です。", "21時15分です。"),
]

ng = 0
for text, want in CASES:
    got = app.clean_reply(text)
    mark = "OK" if got == want else "NG"
    ng += got != want
    print("%s %-52r -> %r" % (mark, text, got))
    if got != want:
        print("   期待: %r" % want)
print("\n%d/%d 正解" % (len(CASES) - ng, len(CASES)))
