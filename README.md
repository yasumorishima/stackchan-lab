# stackchan-lab

M5 スタックちゃん（**M5STACK-K151** / M5Stack CoreS3）の開発記録と作業用ツール。

セットアップで実際に踏んだ問題と、その解決手順を残しています。同じところで止まっている人の役に立てば幸いです。

**解説記事**: [日本語 (Qiita)](https://qiita.com/yasumorishima/items/ef4951ed8483fd8f34dd) / [English (DEV.to)](https://dev.to/yasumorishima/m5-stackchan-pairing-fails-with-no-devices-found-the-factory-firmware-is-the-cause-387e)

> **時期についての注記**: 本体を入手したのは **2026-07-24**（スイッチサイエンス）、セットアップ作業は **2026-07-25** です。この時点の**出荷時ファームウェアは 1.2.4**（2026-04-20 公開）、**公式最新は 1.4.4**（2026-07-13 公開）でした。本製品は 2026-05 発売の新製品で、ファームウェアもアプリも更新が続いています。**入手時期によって出荷時のファーム版数は変わる**ため、下記の症状に当たるかどうかも時期によって変わります。

## 環境

| | |
|---|---|
| 本体 | M5StackChan AI デスクトップロボット（ESP32-S3 搭載） / `M5STACK-K151`（2026-07-24 入手） |
| コントローラ | M5Stack CoreS3（ESP32-S3、16MB Flash、8MB PSRAM） |
| サーボ | FEETECH SCS0009 シリアルバスサーボ ×2（UART、GPIO6/7） |
| バッテリー | 550mAh（ベース側） |
| 母艦 | Windows 11。USB 接続すると USB-Serial/JTAG（`VID_303A` / `PID_1001`）としてシリアルポートに現れる |
| 出荷時ファーム | XiaoZhi ベース（プロジェクト名 `stack-chan`） |

## セットアップでつまずいた点と解決

### 症状: アプリで ID を選んだ後に `No devices found`

スマートフォンアプリ「StackChan World」で本体を選択すると `No devices found` になり、初期設定が完了しない。Bluetooth と位置情報の権限付与、アプリの再起動、端末の再起動、OS 側の Bluetooth 画面からの確認をいくら繰り返しても変わらなかった。

USB シリアルのログを取ったところ、**実際には BLE 接続は成立していた**ことが分かりました。

```
I (111140) NimBLE: connection established; status=0
[info] [WifiConfigServer] app Connected
I (112260) NimBLE: Config data received (42 bytes): {"cmd":"handshake","data":"1784964126892"}
I (112440) NimBLE: Config notification sent
```

ハンドシェイクを受け取り、本体は応答も返しています。**その直後にアプリ側が沈黙する**（切断ログすら出ない）。つまりこれはデバイス探索の失敗ではなく、**ハンドシェイク応答の検証段階での失敗**でした。

アプリ側の実装（[m5stack/StackChan](https://github.com/m5stack/StackChan) にアプリのソースも同居しています）を読むと、接続 30 秒 / 検証 20 秒のタイムアウトを持ち、本体から返る暗号化データを検証してから、サーバーへデバイスを登録する流れになっています。検証に失敗すると先へ進みません。

### 原因: 出荷時ファームウェアが古かった（2026-07-25 時点）

- 実機のファーム: **1.2.4**（2026-04-20 公開）
- 作業日時点の公式最新: **1.4.4**（2026-07-13 公開）

9 バージョン分の開きがありました。さらに構造的な問題として、

**新しいファームは OTA で入る → OTA には Wi-Fi が必要 → Wi-Fi 設定にはアプリのペアリングが必要 → そのペアリングが古いファームで失敗する**

という循環になっており、**USB からの書き込み以外に更新経路がありません**。

**1.4.4 を USB で書き込んだら一度で通り**、AI エージェント設定 → Wi-Fi 設定 → 音声での会話成立まで完走しました。

> **補足: ファームを自分でビルドしても解決しません。** 公開リポジトリの `firmware/main/hal/utils/secret_logic/secret_logic.cpp` はスタブで、ハンドシェイクトークンを返す関数が固定文字列を返すだけです（weak シンボルとして宣言され、公式ビルド時に実装が差し込まれる構造）。自前ビルドではアプリの検証を通らないため、**公式バイナリが必要**です。

### 手順: M5Burner を使わずに USB で公式ファームを書き込む

M5Burner（GUI）のファームウェア配信 API は公開されているため、CLI だけで完結できます。

- 手順の詳細 → [docs/firmware-flash.md](docs/firmware-flash.md)
- 自動化スクリプト → [tools/flash-official-firmware.ps1](tools/flash-official-firmware.ps1)

## ハマりどころメモ

- **シリアルポートを開くと本体が再起動する**（`rst:0x15 (USB_UART_CHIP_RESET)`）。ログ取得と本体操作は同時にできないので、ペアリング等の操作中はポートを開かないこと
- **インタラクティブなシリアルコンソールは無い**。コマンドを送っても応答はなく、書き込み自体がブロックする。シリアルは読み取り専用として扱う
- **OS の Bluetooth 設定からペア設定してはいけない**。ボンディングするとアプリ側の「新しいデバイスを探す」一覧から外れる。誤って登録した場合は削除する
- **2 つの USB-C ポートに同時給電しない**。CoreS3 側から入れた 5V は M-BUS の `BUS_5V` を通ってベース側へ回るため、両方に電源を挿すと同一レールで 2 電源が衝突する。公式ドキュメントの「双方から給電を試す」は片方ずつという意味
- **本体だけで Wi-Fi を設定する画面は無い**。セットアップ画面（`app_setup/workers/connectivity.cpp`）はアプリのインストールを促す QR を表示するだけで、アプリは迂回できない
- **サーボのゼロ点はサーボ本体側に保持されている**。フラッシュを全書き換えしても消えなかった（書き込み前後で同じ値が読み出された）
- 標準の AI は **XiaoZhi**（中国のクラウドサービス）。会話内容は外部のサーバーへ送られる前提で使うこと。LLM は Qwen / Xiaozhi Lite / DeepSeek V4 (Fast) / DouBao Seed 2.0 (Delayed) から選択でき、`Delayed` と付くものは応答が遅いため音声対話には不向き
- 本体は MCP ツールを持っている（起動ログに `self.robot.get_head_angles` / `set_head_angles` / `set_led_color` / `create_reminder` / `get_reminders` / `stop_reminder` が登録される）

## 充電についての注意

ファームウェアは起動時に AXP2101 の充電電流を **700mA** に設定します（`firmware/main/hal/board/stackchan.cc`）。

```cpp
auto ret = setChargerConstantCurr(XPOWERS_AXP2101_CHG_CUR_700MA);
```

ESP32-S3 は USB 2.0 デバイスとして動作するため、**PC の USB ポートから取れるのは 500mA が上限**です。本体の消費（液晶・カメラ・サーボ・無線）と合わせると入力上限を超えるため、**PC 給電では充電が始まりません**。給電には 5V で余裕のある USB 充電器を使ってください。

なお、両サーボが現在位置を返さない（`[ScsServo] ignore invalid current pos: -1`）場合も電力不足のサインです。

## 参考

- 公式ドキュメント: https://docs.m5stack.com/ja/StackChan
- Arduino での開発: https://docs.m5stack.com/ja/arduino/stackchan/program
  - ダウンロードモード = リセットボタンを約 2 秒長押しし、内部の緑色 LED が点灯したら離す
- UIFlow2 での開発: https://docs.m5stack.com/ja/uiflow2/stackchan/program
- ファーム・アプリ・サーバーのソース: https://github.com/m5stack/StackChan
- 顔の描画ライブラリ: https://github.com/meganetaaan/m5stack-avatar

## 今後やること

- Arduino / PlatformIO での自作スケッチ
- MCP ツールを増やして手元の他システムと連携させる

## ライセンス

MIT
