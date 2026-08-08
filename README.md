# stackchan-lab

M5 スタックちゃん（**M5STACK-K151** / M5Stack CoreS3）の開発記録と作業用ツール。

セットアップで実際に踏んだ問題、その原因の突き止め方、再現できる手順を残しています。

**解説記事**: [日本語 (Qiita)](https://qiita.com/yasumorishima/items/ef4951ed8483fd8f34dd) / [English (DEV.to)](https://dev.to/yasumorishima/m5-stackchan-pairing-fails-with-no-devices-found-the-factory-firmware-is-the-cause-387e)

> **時期についての注記**: 本体を入手したのは **2026-07-24**（スイッチサイエンス）、最初のセットアップは **2026-07-25** です。この時点の**出荷時ファームウェアは 1.2.4**（2026-04-20 公開）、**公式最新は 1.4.4**（2026-07-13 公開）でした。本製品は 2026-05 発売の新製品で、ファームウェアもアプリも更新が続いています。**入手時期によって出荷時のファーム版数は変わる**ため、下記の症状に当たるかどうかも時期によって変わります。

## いまできること

| | |
|---|---|
| 会話 | 声で聞いて、声で返します。本体の接続先を[自前サーバー](server/)へ向けているので、中国のクラウドは経由しません |
| 認識 | sherpa-onnx + ReazonSpeech k2 v2（Raspberry Pi 5 上のローカル処理） |
| 応答 | さくらのAI Engine `gpt-oss-120b`（月 3,000 リクエストまで無償） |
| 読み上げ | Open JTalk（ローカル）。長い呼気段落は分けて合成し、読みが遅ければ実測して分け直します。**本体の小さいスピーカーで歌と同じ大きさに聞こえるよう、鳴らない低音を落としてから音量を合わせます** |
| 応答の速さ | **返答は書き終わるのを待たず、1 文できた時点で読み上げ始めます**。聞き取りに渡すのも「いま終わった発話 1 回ぶん」だけ |
| 道具 | サーバー側 19 個（天気・週間天気・為替・株価指数・暗号資産・ニュース・地震・警報・台風・熱中症・電車・今日は何の日・月齢・燃油サーチャージ・渡航情報・プロ野球の速報と順位・出場選手の登録抹消・選手応援歌の歌詞・**応援歌を旋律つきで歌う**）＋ 本体側 11 個（画面・サーボ・カメラなど） |
| 歌える曲 | **16 曲**。歌唱音源から起こした 12 曲に加え、音源が配られていない選手は**ゲームの応援歌エディタの譜面から起こした旋律**で歌います |
| 割り込み | 読み上げの途中で話しかけると、残りをやめて聞き直します |

どう作ったか、何を測って決めたかは **[docs/progress.md](docs/progress.md)** に日付順で残しています。

## ドキュメント

索引は [docs/README.md](docs/README.md) にあります。

| ページ | 内容 |
|---|---|
| [初期設定とペアリング不能問題](docs/setup/pairing-and-firmware.md) | アプリで `No devices found` が出て設定が終わらない問題。シリアルログでの切り分けから、原因（出荷時ファームが古い）と解決までの全記録。踏んだ罠の一覧つき |
| [公式ファームウェアを USB で書き込む](docs/setup/firmware-flash.md) | M5Burner（GUI）を使わず、公開 API と `esptool` 単体実行ファイルで公式バイナリを書き込む手順。ロールバック方法も記載 |
| [音声バックエンド差し替えの検討](docs/voice/voice-backend-plan.md) | 出荷時は中国のクラウド XiaoZhi 経由。自前サーバーへ寄せる設計と、無償枠・自前ビルド・プロビジョニングの前提整理。差し替えポイントと現在の進捗 |
| [XiaoZhi WebSocket プロトコル](docs/voice/xiaozhi-websocket-protocol.md) | 自前サーバーを書くために必要な仕様のまとめ。OTA 応答スキーマ、hello の交換、音声フレームの形式、JSON メッセージの一覧 |
| [自前サーバーの実装](server/) | 本体の接続先をこのサーバーへ向けるための実装一式。OTA と WebSocket、ローカル音声認識、VOICEVOX での読み上げ、MCP のツール呼び出し。実機なしで検証するための試験クライアントとモックつき |

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

## 今後やること

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
