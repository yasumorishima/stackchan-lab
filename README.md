# stackchan-lab

M5 スタックちゃん（**M5STACK-K151** / M5Stack CoreS3）の開発記録と作業用ツール。

セットアップで実際に踏んだ問題、その原因の突き止め方、再現できる手順を残しています。

**解説記事**: [日本語 (Qiita)](https://qiita.com/yasumorishima/items/ef4951ed8483fd8f34dd) / [English (DEV.to)](https://dev.to/yasumorishima/m5-stackchan-pairing-fails-with-no-devices-found-the-factory-firmware-is-the-cause-387e)

> **時期についての注記**: 本体を入手したのは **2026-07-24**（スイッチサイエンス）、最初のセットアップは **2026-07-25** です。この時点の**出荷時ファームウェアは 1.2.4**（2026-04-20 公開）、**公式最新は 1.4.4**（2026-07-13 公開）でした。本製品は 2026-05 発売の新製品で、ファームウェアもアプリも更新が続いています。**入手時期によって出荷時のファーム版数は変わる**ため、下記の症状に当たるかどうかも時期によって変わります。

## ドキュメント

索引は [docs/README.md](docs/README.md) にあります。

| ページ | 内容 |
|---|---|
| [初期設定とペアリング不能問題](docs/setup/pairing-and-firmware.md) | アプリで `No devices found` が出て設定が終わらない問題。シリアルログでの切り分けから、原因（出荷時ファームが古い）と解決までの全記録。踏んだ罠の一覧つき |
| [公式ファームウェアを USB で書き込む](docs/setup/firmware-flash.md) | M5Burner（GUI）を使わず、公開 API と `esptool` 単体実行ファイルで公式バイナリを書き込む手順。ロールバック方法も記載 |
| [音声バックエンド差し替えの検討](docs/voice/voice-backend-plan.md) | 出荷時は中国のクラウド XiaoZhi 経由。自前サーバーへ寄せる設計と、無償枠・自前ビルド・プロビジョニングの前提整理。差し替えポイントと現在の進捗 |
| [XiaoZhi WebSocket プロトコル](docs/voice/xiaozhi-websocket-protocol.md) | 自前サーバーを書くために必要な仕様のまとめ。OTA 応答スキーマ、hello の交換、音声フレームの形式、JSON メッセージの一覧 |

## 要点だけ先に

初期設定でアプリが `No devices found` を返す場合、**Bluetooth の権限ではなく出荷時ファームウェアが古いことが原因**の可能性があります。シリアルログを見ると BLE 接続とハンドシェイクは成功していて、アプリ側が応答の検証段階で止まっていました。

さらに **新ファームは OTA → OTA には Wi-Fi → Wi-Fi 設定にはアプリのペアリング** という循環になっており、**USB 書き込み以外に出口がありません**。詳細は [初期設定とペアリング不能問題](docs/setup/pairing-and-firmware.md) を参照してください。

## ツール

| ツール | 用途 |
|---|---|
| [tools/flash-official-firmware.ps1](tools/flash-official-firmware.ps1) | 公式ファームの一覧取得・ダウンロード・検証・書き込みを自動化。既定はドライラン（取得と通信確認のみ）で、`-Flash` を付けたときだけ書き込む |

## この機体について

| | |
|---|---|
| 本体 | M5StackChan AI デスクトップロボット（ESP32-S3 搭載） / `M5STACK-K151`（2026-07-24 入手） |
| コントローラ | M5Stack CoreS3（ESP32-S3、16MB Flash、8MB PSRAM） |
| サーボ | FEETECH SCS0009 シリアルバスサーボ ×2（UART、GPIO6/7） |
| バッテリー | 550mAh（ベース側） |
| 母艦 | Windows 11。USB 接続すると USB-Serial/JTAG（`VID_303A` / `PID_1001`）としてシリアルポートに現れる |
| 出荷時ファーム | XiaoZhi ベース（プロジェクト名 `stack-chan`） |

## 進捗

**音声バックエンドの差し替え（2026-07-26）**: プロトコルの読み取りと、自前サーバー（OTA エンドポイント + WebSocket）の実装・検証まで終わりました。実機を使わず、本体を模した試験クライアントで次が通ることを確認しています。

- OTA が現在版と同じ `version` を返し、更新を走らせないこと
- サーバーの `hello`（`transport` は `websocket` 厳密一致）を返せること
- 上り Opus（16000Hz mono / 60ms）を復号できること
- `stt` / `llm` / `tts` を送り、下り Opus（24000Hz）を生成できること

本体の接続先を切り替えるための NVS イメージ（`wifi` 名前空間に `ota_url`、16KB）も生成済みです。ただし書き込むと既存の Wi-Fi 設定とアプリの紐付けが消えて再ペアリングが必要になるため、まだ書いていません。サーバーの実装もまだこのリポジトリには入れていません。

## 今後やること

- 音声バックエンドの差し替えを実機で通す（→ [検討メモ](docs/voice/voice-backend-plan.md)）
- Arduino / PlatformIO での自作スケッチ
- MCP ツールを増やして手元の他システムと連携させる

## 参考

- 公式ドキュメント: https://docs.m5stack.com/ja/StackChan
- Arduino での開発: https://docs.m5stack.com/ja/arduino/stackchan/program
- UIFlow2 での開発: https://docs.m5stack.com/ja/uiflow2/stackchan/program
- ファームウェア・アプリ・サーバーのソース: https://github.com/m5stack/StackChan
- 顔の描画ライブラリ: https://github.com/meganetaaan/m5stack-avatar

## ライセンス

MIT
