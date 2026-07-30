"""hello を送らない本体に対して、サーバーが先に server hello を出すかを見る。

本体は自分の hello を送ってから server hello を 10 秒待つ実装なので、
往路の hello が落ちた時にこれが無いと黙って切られる。

  ./.venv/bin/python test_hello_grace.py
"""
import asyncio
import json
import os
import socket
import sys
import time

HOST = os.environ.get("HOST", "192.168.10.111")
PORT = int(os.environ.get("PORT", "8000"))

REQ = (
    "GET /ws HTTP/1.1\r\n"
    "Host: " + HOST + "\r\n"
    "Client-Id: hello-grace-test\r\n"
    "Connection: Upgrade\r\n"
    "Device-Id: 80:45:6b:4d:e9:84\r\n"
    "Protocol-Version: 1\r\n"
    "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
    "Sec-WebSocket-Version: 13\r\n"
    "Upgrade: websocket\r\n"
    "\r\n"
)


def main() -> int:
    s = socket.create_connection((HOST, PORT), timeout=15)
    t0 = time.monotonic()
    s.sendall(REQ.encode("ascii"))

    buf = b""
    while b"\r\n\r\n" not in buf:
        buf += s.recv(4096)
    buf = buf[buf.index(b"\r\n\r\n") + 4:]

    # ここで hello を送らずに黙って待つ（往路が落ちた本体の再現）
    s.settimeout(15)
    while len(buf) < 2:
        buf += s.recv(4096)
    took = time.monotonic() - t0
    ln = buf[1] & 0x7F
    off = 2
    if ln == 126:
        ln = (buf[2] << 8) | buf[3]
        off = 4
    while len(buf) < off + ln:
        buf += s.recv(4096)
    data = json.loads(buf[off:off + ln].decode("utf-8"))
    s.close()

    print("%.2f 秒で受信: %s" % (took, json.dumps(data, ensure_ascii=False)))
    ok = data.get("type") == "hello" and data.get("transport") == "websocket"
    ok = ok and took < 10          # 本体の待ち時間 10 秒より前に届くこと
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


sys.exit(main())
