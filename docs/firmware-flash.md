# M5Burner を使わずに公式ファームウェアを USB 書き込みする

M5Burner（GUI）が使うファームウェア配信 API は公開されているため、CLI だけで公式バイナリを取得して書き込めます。Python も不要です（Espressif が単体実行ファイルの esptool を配布しています）。

前提: 本体を USB ケーブルで PC に接続し、シリアルポート（例: `COM3`）として見えている状態。

## 1. 公式ファームの一覧を取得する

```bash
curl -s -m 40 "https://m5burner-api.m5stack.com/api/firmware" \
  | jq -r '.[] | select(.name=="StackChan-UserDemo") | .versions[] | "\(.version)  \(.published_at)  \(.file)"'
```

出荷時ファームウェアは **`StackChan-UserDemo`**（author: M5Stack）です。出力例:

```
V1.2.4   2026-04-20  3c8ffe6be0ca26375d836e10e06e3609.bin
V1.4.3   2026-07-02  fb75fa818e63b7ee6b0d35eba308f386.bin
V1.4.4   2026-07-13  790e3fcde496020aa7f188153b23e6f0.bin
```

同じ API には他のカテゴリのファームも入っているので、`select()` で絞ります。旧バージョンも残っているため、**書き戻し（ロールバック）も可能**です。

## 2. バイナリをダウンロードする

```bash
curl -sL -m 300 "https://m5burner-cdn.m5stack.com/firmware/790e3fcde496020aa7f188153b23e6f0.bin" \
  -o stackchan-v1.4.4.bin
```

先頭バイトが `e9`（ESP イメージのマジックナンバー）であることを確認します。オフセット `0x0` から書くマージ済みのフルイメージです。

```bash
od -An -tx1 -N 1 stackchan-v1.4.4.bin
#  e9
```

## 3. esptool を用意する（Python 不要）

[espressif/esptool のリリース](https://github.com/espressif/esptool/releases/latest) に単体実行ファイルの zip があります。

```bash
gh api repos/espressif/esptool/releases/latest \
  --jq '.assets[] | select(.name|test("windows-amd64")) | .browser_download_url'
```

展開すると `esptool-windows-amd64/esptool.exe` が入っています。

## 4. 通信できるか確認する（読み取りのみ）

書き込む前に、無害なコマンドでチップと通信できることを確かめます。

```powershell
.\esptool.exe --port COM3 flash-id
```

期待される出力:

```
Detecting chip type... ESP32-S3
Chip type:          ESP32-S3 (QFN56) (revision v0.2)
Features:           Wi-Fi, BT 5 (LE), Dual Core + LP Core, 240MHz
USB mode:           USB-Serial/JTAG
Detected flash size: 16MB
```

**ダウンロードモードへの手動操作は通常不要です**（USB-Serial/JTAG 経由で esptool が自動リセットします）。接続できない場合のみ、リセットボタンを約 2 秒長押しし、内部の緑色 LED が点灯したら離してダウンロードモードに入れてください。

## 5. 書き込む

```powershell
.\esptool.exe --port COM3 write-flash 0x0 stackchan-v1.4.4.bin
```

12.8MB 弱で 50 秒程度です。最後に `Hash of data verified.` が出れば成功です。

```
Wrote 12783792 bytes (3185393 compressed) at 0x00000000 in 47.7 seconds
Verifying written data...
Hash of data verified.
Hard resetting via RTS pin...
```

## 6. バージョンを確認する

シリアルポートを開いて起動ログを読みます。ポートを開くと本体は再起動するので、そのまま先頭から読めます。

```powershell
$p = New-Object System.IO.Ports.SerialPort("COM3", 115200)
$p.ReadTimeout = 1000
$p.Open()
$p.RtsEnable = $true; Start-Sleep -Milliseconds 150; $p.RtsEnable = $false   # リセット
$end = (Get-Date).AddSeconds(20)
while ((Get-Date) -lt $end) { try { $p.ReadLine() } catch {} }
$p.Close()
```

次の行が出れば完了です。

```
I (696) app_init: App version:      1.4.4
```

## 注意点

- **フラッシュ全体を書き換えるため NVS（設定領域）も初期化されます。** ただしサーボのゼロ点はサーボ本体側に保存されているため失われません（実測で書き込み前後とも同じ値が読めました）。Wi-Fi 設定済みの機体では Wi-Fi 情報が消えるので、再設定が必要になります
- 書き込み中に USB ケーブルを抜かないこと。転送が途中で止まると一時的に起動しなくなりますが、ダウンロードモードに入れて再度書き込めば復旧します
- `--baud 921600` を付けた 16MB 全体の読み出しは途中で `Packet content transfer stopped` になりました。バックアップを取る場合は既定の速度か、必要な領域だけに絞るのが無難です
- 給電が細いと転送が不安定になります。充電器から給電した状態で作業するのが安全です
