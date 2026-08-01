"""本番と同じ「システム文 + tools」でひと往復して、接頭辞をキャッシュに載せる。

/api/generate の warm では chat テンプレートも tools も通らないため、
最初の発話だけ全量 prefill になって遅い（実測 35s）。
"""
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app  # noqa: E402
import server_tools  # noqa: E402

BASE = os.environ.get("SAKURA_BASE", "http://127.0.0.1:11434")
MODEL = os.environ.get("SAKURA_MODEL", "qwen2.5:3b")

DEVICE_TOOL = {
    "type": "function",
    "function": {
        "name": "self_audio_speaker_set_volume",
        "description": "スピーカーの音量を 0-100 で設定する",
        "parameters": {"type": "object",
                       "properties": {"volume": {"type": "integer"}},
                       "required": ["volume"]},
    },
}

payload = {
    "model": MODEL,
    "messages": [{"role": "system", "content": app.system_prompt()},
                 {"role": "user", "content": "こんにちは。"}],
    "temperature": 0.7, "max_tokens": 16, "stream": False,
    "tools": list(server_tools.specs()) + [DEVICE_TOOL],
    "tool_choice": "auto",
}
req = urllib.request.Request(BASE + "/v1/chat/completions",
                             data=json.dumps(payload).encode("utf-8"),
                             method="POST")
req.add_header("Content-Type", "application/json")
req.add_header("Authorization", "Bearer " + os.environ.get("SAKURA_TOKEN", "dummy"))
t0 = time.monotonic()
with urllib.request.urlopen(req, timeout=300) as r:
    r.read()
print("warm: %.1fs" % (time.monotonic() - t0))
