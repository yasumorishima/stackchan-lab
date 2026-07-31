# -*- coding: utf-8 -*-
"""本体（スタックちゃん）の接続先を 自前サーバー⇔公式 で切り替えるための NVS 編集。

wifi/ota_url エントリだけを触る（Wi-Fi・MQTT 資格情報・アプリ紐付けは無変更）。
  status      <in>              現在の向き先を表示（token 等の秘密は出さない）
  to-ours     <in> <out> <url>  ota_url を追記。既に同値なら no-op、別値なら消去して追記
  to-official <in> <out>        ota_url を消去（状態ビット 0b00）。
                                Ota::GetCheckVersionUrl() は空なら CONFIG_OTA_URL（公式）へ落ちる

このファイルの掟（2026-07-30〜31 に実際に踏んだ罠）:
1. CRC 式は推測しない。既存エントリ全件を再現できた式だけ採用する。
2. 鍵フィールド（16B）の余りは 0x00 で埋める。0xFF のまま残すとハッシュ索引が
   永久に外れて ESP_ERR_NVS_NOT_FOUND になる（2026-07-31 の真因）。
3. 出力は必ず Espressif 公式の nvs_tool で検証してから書き込む
   （自作パーサは自分のバグに気づけない）。
"""
import binascii
import struct
import sys

PAGE = 4096
ENTRY = 32
N_ENTRY = 126
HDR = 64            # ページヘッダ 32 + ビットマップ 32
ACTIVE = 0xFFFFFFFE
UNINIT = 0xFFFFFFFF
T_U8 = 0x01
T_STR = 0x21


def crc_le(crc, buf):
    """ESP-IDF の crc32_le 相当（入力 crc を反転して渡す流儀）。"""
    return binascii.crc32(buf, crc ^ 0xFFFFFFFF) ^ 0xFFFFFFFF


def crc_plain(crc, buf):
    return binascii.crc32(buf, crc)


CANDS = {"esp_crc32_le": crc_le, "zlib_plain": crc_plain}


def item_crc(fn, ent):
    """エントリ 32 バイトから crc32 欄（4..8）を除いた部分の CRC。"""
    c = fn(0xFFFFFFFF, ent[0:4])
    c = fn(c, ent[8:32])
    return c & 0xFFFFFFFF


def pages(data):
    for p in range(len(data) // PAGE):
        yield p, p * PAGE


def bit(bitmap, i):
    return (bitmap[i // 4] >> ((i % 4) * 2)) & 0b11


def key_of(ent):
    """鍵 16B を最初の 0x00 か 0xFF で切って返す（0xFF 埋めの不良エントリも拾う）。"""
    raw = ent[8:24]
    out = bytearray()
    for b in raw:
        if b in (0x00, 0xFF):
            break
        out.append(b)
    return bytes(out).decode("utf-8", "replace")


def written_entries(data):
    out = []
    for p, base in pages(data):
        state = struct.unpack_from("<I", data, base)[0]
        if state == UNINIT:
            continue
        bm = data[base + 32:base + 64]
        i = 0
        while i < N_ENTRY:
            if bit(bm, i) != 0b10:
                i += 1
                continue
            off = base + HDR + i * ENTRY
            ent = data[off:off + ENTRY]
            out.append((p, base, i, off, ent))
            i += max(1, ent[2])
    return out


def pick_formula(data):
    ents = written_entries(data)
    best = None
    for name, fn in CANDS.items():
        ok = sum(1 for *_, e in ents
                 if item_crc(fn, e) == struct.unpack_from("<I", e, 4)[0])
        print("  CRC 式 %-14s 一致 %d/%d" % (name, ok, len(ents)))
        if ok == len(ents):
            best = (name, fn)
    return best, len(ents)


def find_ns_index(data, ns_name):
    for *_, e in written_entries(data):
        if e[0] == 0 and e[1] == T_U8 and key_of(e) == ns_name:
            return e[24]
    return None


def find_items(data, ns_index, key):
    return [(p, base, i, off, e) for p, base, i, off, e in written_entries(data)
            if e[0] == ns_index and key_of(e) == key]


def read_str(data, off, ent):
    size = struct.unpack_from("<H", ent, 24)[0]
    raw = data[off + ENTRY:off + ENTRY + size]
    return raw.split(b"\x00")[0].decode("utf-8", "replace")


def erase_item(data, base, idx, span):
    """状態ビットを 0b00（消去済み）にする。エントリ本体はそのまま。"""
    for i in range(idx, idx + span):
        bpos = base + 32 + i // 4
        shift = (i % 4) * 2
        data[bpos] = data[bpos] & ~(0b11 << shift) & 0xFF


def append_str(data, fn, nsi, key, value):
    raw = value.encode("utf-8") + b"\x00"
    n_data = (len(raw) + ENTRY - 1) // ENTRY
    span = 1 + n_data
    target = None
    for p, base in pages(data):
        state = struct.unpack_from("<I", data, base)[0]
        if state != ACTIVE:
            continue
        bm = data[base + 32:base + 64]
        run = 0
        start = None
        for i in range(N_ENTRY):
            if bit(bm, i) == 0b11:
                if start is None:
                    start = i
                run += 1
                if run >= span:
                    target = (p, base, start)
                    break
            else:
                start = None
                run = 0
        if target:
            break
    if not target:
        print("FAIL: ACTIVE ページに連続した空きが無い")
        sys.exit(1)
    p, base, idx = target
    print("追記先 = ページ %d のエントリ %d から %d 個" % (p, idx, span))

    payload = raw + b"\xff" * (n_data * ENTRY - len(raw))
    dcrc = fn(0xFFFFFFFF, raw) & 0xFFFFFFFF

    ent = bytearray(b"\xff" * ENTRY)
    ent[0] = nsi
    ent[1] = T_STR
    ent[2] = span
    ent[3] = 0xFF                      # chunk_index（文字列では CHUNK_ANY）
    kb = key.encode("utf-8")
    # 🔴 鍵は 16B を 0x00 で埋め切る（ファームの strncpy と同じ形にしないと
    #    ハッシュ索引が一致せず永久に見つからない）
    ent[8:24] = kb + bytes(16 - len(kb))
    struct.pack_into("<HHI", ent, 24, len(raw), 0xFFFF, dcrc)
    struct.pack_into("<I", ent, 4, item_crc(fn, bytes(ent)))

    off = base + HDR + idx * ENTRY
    data[off:off + ENTRY] = ent
    data[off + ENTRY:off + ENTRY + len(payload)] = payload
    for i in range(idx, idx + span):
        bpos = base + 32 + i // 4
        data[bpos] = data[bpos] & ~(0b01 << ((i % 4) * 2)) & 0xFF

    # 書いた直後に自己検証（鍵パディング・CRC）
    e2 = data[off:off + ENTRY]
    assert e2[8:24] == kb + bytes(16 - len(kb)), "鍵パディングが 0x00 埋めでない"
    assert item_crc(fn, e2) == struct.unpack_from("<I", e2, 4)[0], "項目 CRC 不一致"


def show_status(data):
    nsi_wifi = find_ns_index(data, "wifi")
    nsi_ws = find_ns_index(data, "websocket")
    items = find_items(data, nsi_wifi, "ota_url") if nsi_wifi is not None else []
    if items:
        for p, base, i, off, e in items:
            pad = e[8:24][len(b"ota_url"):]
            pad_ok = all(b == 0 for b in pad)
            print("wifi/ota_url = %s" % read_str(data, off, e))
            print("  鍵パディング: %s" % ("0x00 埋め (正常)" if pad_ok else "🔴 0xFF 混入 (ハッシュ索引に載らない不良)"))
        url = read_str(data, items[0][3], items[0][4])
        if "tenclass.net" in url:
            print("=> 向き先: 公式（tenclass）を明示指定")
        else:
            print("=> 向き先: 自前サーバー")
    else:
        print("wifi/ota_url = (無し)")
        print("=> 向き先: 公式（コンパイル既定 https://api.tenclass.net/xiaozhi/ota/）")
    if nsi_ws is not None:
        ws = find_items(data, nsi_ws, "url")
        if ws:
            print("websocket/url = %s （参考: OTA 応答で毎回上書きされる）" % read_str(data, ws[0][3], ws[0][4]))


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    mode, src = sys.argv[1], sys.argv[2]
    data = bytearray(open(src, "rb").read())
    if len(data) % PAGE != 0:
        print("FAIL: サイズが %d＝ページ境界に合わない" % len(data))
        sys.exit(1)

    if mode == "status":
        show_status(data)
        return

    dst = sys.argv[3]
    print("CRC 式を既存エントリで実測:")
    best, n = pick_formula(data)
    if not best:
        print("FAIL: 既存エントリを再現できる CRC 式が無い（書き込みは危険）")
        sys.exit(1)
    name, fn = best
    print("  -> 採用: %s（既存 %d 件すべて一致）" % (name, n))

    nsi = find_ns_index(data, "wifi")
    if nsi is None:
        print("FAIL: 名前空間 wifi が無い")
        sys.exit(1)

    items = find_items(data, nsi, "ota_url")

    if mode == "to-official":
        if not items:
            print("ota_url は元々無い＝既に公式向き。何もしない")
            open(dst, "wb").write(bytes(data))
            return
        for p, base, i, off, e in items:
            span = max(1, e[2])
            print("ページ %d エントリ %d から span %d を消去（0b00）" % (p, i, span))
            erase_item(data, base, i, span)
    elif mode == "to-ours":
        url = sys.argv[4]
        keep = False
        for p, base, i, off, e in items:
            pad = e[8:24][len(b"ota_url"):]
            pad_ok = all(b == 0 for b in pad)
            if read_str(data, off, e) == url and pad_ok and not keep:
                keep = True
                continue
            span = max(1, e[2])
            print("旧エントリ（ページ %d エントリ %d）を消去してから追記" % (p, i))
            erase_item(data, base, i, span)
        if keep:
            print("既に同じ URL が正しい形で入っている＝何もしない")
            open(dst, "wb").write(bytes(data))
            return
        append_str(data, fn, nsi, "ota_url", url)
    else:
        print("FAIL: 不明なモード %r" % mode)
        sys.exit(2)

    open(dst, "wb").write(bytes(data))
    print("出力 %s （%d バイト）" % (dst, len(data)))
    print("--- 出力の status ---")
    show_status(bytearray(open(dst, "rb").read()))


if __name__ == "__main__":
    main()
