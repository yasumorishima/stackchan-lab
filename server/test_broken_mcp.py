"""壊れた mcp メッセージ 1 通で対話ごと落ちないことを見る。

本体から来る mcp の id は本来 int だが、壊れた値（配列など）が来ると
待ち行列の引き当てが unhashable で例外になる。受信ループの中で直接呼んで
いるので、握り潰さないと接続ごと落ちる。

  ./.venv/bin/python test_broken_mcp.py
"""
import json
import os
import socket
import sys

HOST = os.environ.get("HOST", "192.168.10.111")
PORT = int(os.environ.get("PORT", "8000"))

REQ = (
    "GET /ws HTTP/1.1\r\n"
    "Host: " + HOST + "\r\n"
    "Client-Id: broken-mcp-test\r\n"
    "Connection: Upgrade\r\n"
    "Device-Id: 80:45:6b:4d:e9:84\r\n"
    "Protocol-Version: 1\r\n"
    "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
    "Sec-WebSocket-Version: 13\r\n"
    "Upgrade: websocket\r\n"
    "\r\n"
)

HELLO = ('{"type":"hello","version":1,"features":{"mcp":true},'
         '"transport":"websocket","audio_params":{"format":"opus",'
         '"sample_rate":16000,"channels":1,"frame_duration":60}}')
BAD_MCP = '{"type":"mcp","payload":{"id":[1,2],"result":{}}}'


def frame(payload: bytes) -> bytes:
    out = bytearray([0x81])
    n = len(payload)
    if n < 126:
        out.append(0x80 | n)
    else:
        out += bytes([0x80 | 126, (n >> 8) & 0xFF, n & 0xFF])
    m = os.urandom(4)
    out += m
    out += bytes(payload[i] ^ m[i % 4] for i in range(n))
    return bytes(out)


def read_frame(s, buf):
    while len(buf) < 2:
        buf += s.recv(4096)
    ln = buf[1] & 0x7F
    off = 2
    if ln == 126:
        ln = (buf[2] << 8) | buf[3]
        off = 4
    while len(buf) < off + ln:
        buf += s.recv(4096)
    return buf[off:off + ln], buf[off + ln:]


def main() -> int:
    s = socket.create_connection((HOST, PORT), timeout=10)
    s.sendall(REQ.encode("ascii"))
    buf = b""
    while b"\r\n\r\n" not in buf:
        buf += s.recv(4096)
    buf = buf[buf.index(b"\r\n\r\n") + 4:]

    s.sendall(frame(HELLO.encode()))
    s.settimeout(10)
    payload, buf = read_frame(s, buf)
    if json.loads(payload).get("type") != "hello":
        print("FAIL: server hello が来ない")
        return 1

    s.sendall(frame(BAD_MCP.encode()))

    # 生きていれば何も来ないまま待たされる。落ちていれば即座に切断される
    alive = False
    s.settimeout(5)
    try:
        while True:
            d = s.recv(4096)
            if not d:
                print("FAIL: 壊れた mcp で接続ごと落ちた")
                break
    except socket.timeout:
        alive = True
    except OSError as e:
        print("FAIL: 接続が切れた (%s)" % type(e).__name__)
    s.close()

    print("接続は維持された" if alive else "接続が切れた")
    print("PASS" if alive else "FAIL")
    return 0 if alive else 1


sys.exit(main())
