"""既定経路の読み取り（_default_gateway / _primary_ipv4）の単体試験。

経路表を差し替えて判定だけを見るので、ネットワークにも本体にも依存しない。

  ./.venv/bin/python test_gateway.py
"""
import builtins
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app

HEAD = ("Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask"
        "\t\tMTU\tWindow\tIRTT\n")
# 実機（Raspberry Pi 5 / wlan0）で観測した既定経路。GW は little endian の hex
REAL = "wlan0\t00000000\t010AA8C0\t0003\t0\t0\t600\t00000000\t0\t0\t0\n"
# point-to-point（ppp0 等）はゲートウェイを持たないので GW が 0 になる
PPP = "ppp0\t00000000\t00000000\t0001\t0\t0\t0\t00000000\t0\t0\t0\n"
# VPN の redirect-gateway が張る 0.0.0.0/1。宛先だけ見ると既定経路と紛らわしい
VPN_HALF = "tun0\t00000000\t0100000A\t0003\t0\t0\t50\t00000080\t0\t0\t0\n"
SHORT = "bad\t00000000\t010AA8C0\n"

CASES = [
    ("実機どおりの既定経路", HEAD + REAL, "192.168.10.1"),
    ("p2p だけ（ゲートウェイ無し）", HEAD + PPP, ""),
    ("VPN の 0.0.0.0/1 が先にある", HEAD + VPN_HALF + REAL, "192.168.10.1"),
    ("列の足りない行が先にある", HEAD + SHORT + REAL, "192.168.10.1"),
    ("経路表が空", HEAD, ""),
]

_real_open = builtins.open


def _with_table(table, fn):
    def fake_open(path, *a, **kw):
        if str(path) == "/proc/net/route":
            return io.StringIO(table)
        return _real_open(path, *a, **kw)
    builtins.open = fake_open
    try:
        return fn()
    finally:
        builtins.open = _real_open


def main() -> int:
    ok = True
    for name, table, want in CASES:
        got = _with_table(table, app._default_gateway)
        good = got == want
        ok = ok and good
        print("%-30s want=%-14r got=%-14r %s"
              % (name, want, got, "OK" if good else "NG"))

    # ゲートウェイが取れない時に 127.0.0.1 で確定しないこと。
    # Linux は connect("0.0.0.0") を loopback 宛として成功させるため、
    # 0.0.0.0 をそのまま返すとここが 127.0.0.1 になってしまう
    ip = _with_table(HEAD + PPP, app._primary_ipv4)
    good = ip != "127.0.0.1"
    ok = ok and good
    print("%-30s want=%-14s got=%-14r %s"
          % ("取れない時も loopback に落ちない", "not 127.0.0.1", ip,
             "OK" if good else "NG"))

    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


sys.exit(main())
