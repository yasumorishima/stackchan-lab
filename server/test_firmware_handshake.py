"""本体のファームの WebSocket クライアントを写して握手を試す。

78/esp-ml307 の src/web_socket.cc の Connect() / Send() をそのまま真似た:
  - ヘッダは std::map なので ASCII 昇順に並ぶ。Host だけ先頭
  - Host にポートを付けない
  - Sec-WebSocket-Extensions も User-Agent も送らない
  - 握手の判定は "HTTP/1.1 101" を含むかだけ
  - フレームは FIN + opcode、マスクビット、4 バイトマスク、マスク済み本文

鍵は RFC 6455 の例示値を直書きする（ファーム側も未シードの rand() なので毎回同じ）。
これで握手が通るなら「本体が我々の 101 を受け取れない」説は弱くなる。
"""
import json
import os
import socket
import sys

HOST = "192.168.10.111"
PORT = 8000
PATH = "/ws"

headers = {
    "Authorization": "Bearer stackchan-local",
    "Client-Id": "13d7f426-4d5c-4b75-8a2d-03c14de777c0",
    "Connection": "Upgrade",
    "Device-Id": "80:45:6b:4d:e9:84",
    "Protocol-Version": "1",
    "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
    "Sec-WebSocket-Version": "13",
    "Upgrade": "websocket",
}

req = "GET " + PATH + " HTTP/1.1\r\n"
req += "Host: " + HOST + "\r\n"          # ポートを付けない（ファームどおり）
for k in sorted(headers):                 # std::map の並び
    req += k + ": " + headers[k] + "\r\n"
req += "\r\n"

HELLO = ('{"type":"hello","version":1,"features":{"mcp":true},'
         '"transport":"websocket","audio_params":{"format":"opus",'
         '"sample_rate":16000,"channels":1,"frame_duration":60}}')


def mask_frame(payload: bytes, opcode: int = 0x1) -> bytes:
    out = bytearray()
    out.append(0x80 | opcode)
    n = len(payload)
    if n < 126:
        out.append(0x80 | n)
    else:
        out.append(0x80 | 126)
        out.append((n >> 8) & 0xFF)
        out.append(n & 0xFF)
    m = os.urandom(4)
    out += m
    out += bytes(payload[i] ^ m[i % 4] for i in range(n))
    return bytes(out)


s = socket.create_connection((HOST, PORT), timeout=10)
s.sendall(req.encode("ascii"))

buf = b""
while b"\r\n\r\n" not in buf:
    chunk = s.recv(4096)
    if not chunk:
        print("FAIL: 握手応答の途中で切断された")
        sys.exit(1)
    buf += chunk

pos = buf.index(b"\r\n\r\n") + 4
resp, buf = buf[:pos], buf[pos:]
print("--- サーバーの握手応答（%d バイト）---" % len(resp))
print(resp.decode("latin-1").rstrip())
print("--- ここまで ---")

if b"HTTP/1.1 101" not in resp:
    print("FAIL: ファームの判定（HTTP/1.1 101 を含むか）が通らない")
    sys.exit(1)
print("握手判定: OK（ファームと同じ判定で通る）")

s.sendall(mask_frame(HELLO.encode("utf-8")))

s.settimeout(10)
while len(buf) < 2:
    buf += s.recv(4096)
opcode = buf[0] & 0x0F
ln = buf[1] & 0x7F
off = 2
if ln == 126:
    ln = (buf[2] << 8) | buf[3]
    off = 4
while len(buf) < off + ln:
    buf += s.recv(4096)
payload = buf[off:off + ln]
print("server hello: opcode=%d len=%d" % (opcode, ln))
print(payload.decode("utf-8"))
ok = opcode == 1 and json.loads(payload).get("transport") == "websocket"
s.close()
print("PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
