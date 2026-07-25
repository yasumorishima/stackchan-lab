# ドキュメント索引

トピックごとにディレクトリを分けています。増えたらここに追記します。

## setup — 導入・セットアップ

| ページ | 内容 |
|---|---|
| [pairing-and-firmware.md](setup/pairing-and-firmware.md) | 初期設定でアプリが `No devices found` を返す問題。シリアルログでの切り分け、原因（出荷時ファームが古い）、解決までの全記録と、踏んだ罠の一覧 |
| [firmware-flash.md](setup/firmware-flash.md) | 公式ファームウェアを USB で書き込む手順（M5Burner 不要）。バージョン一覧の取得、ロールバック、注意点 |

## voice — 音声・AI バックエンド

| ページ | 内容 |
|---|---|
| [voice-backend-plan.md](voice/voice-backend-plan.md) | 出荷時の XiaoZhi（中国クラウド）から、自前サーバー / 国内 API + VOICEVOX へ寄せる設計検討。差し替えポイント、無償枠の実効値、プロビジョニングの回避策 |
| [xiaozhi-websocket-protocol.md](voice/xiaozhi-websocket-protocol.md) | 自前サーバーを書くためのプロトコル仕様まとめ。NVS 設定キー、必須ヘッダ、hello の交換、バイナリフレーム構造、JSON メッセージ一覧、MCP でロボットを動かす方法、最小要件 |

## 命名と構成の方針

- **トピックごとに `docs/<topic>/` を作る**（`setup` / `voice` / 今後は `dev`（自作スケッチ）や `hardware` など）
- ファイル名は内容が分かる英小文字ケバブケース
- **外部（ブログ等）からのリンクはリポジトリのルートに張る**。個別ファイルへの深いリンクは構成変更で切れるため、ルートの README をハブとして経由させる
- 各ページの冒頭には、いつ時点の情報かを書く（ファームやアプリの更新が速いため）
