# XiaoZhi WebSocket プロトコル（自前サーバー実装用のまとめ）

スタックちゃんの音声パイプラインは XiaoZhi のプロトコルで動きます。**自前サーバーに差し替えるために必要な仕様**を、実装と公式仕様書から読み取ってまとめたものです。

**基準バージョン: [78/xiaozhi-esp32](https://github.com/78/xiaozhi-esp32) `v2.2.4`**（`firmware/repos.json` がこの版を取得して `patches/xiaozhi-esp32.patch` を当てる構成）。出典は上流の `docs/websocket.md` と `main/protocols/websocket_protocol.cc` / `main/ota.cc`。**2026-07-25 時点の内容で、上流の更新で変わりえます。**

## 全体の流れ

1. 本体が **OTA エンドポイント**へ HTTP で問い合わせる（接続先は NVS `wifi/ota_url`、空ならビルド時の `CONFIG_OTA_URL`）
2. OTA 応答の `websocket` セクションが **NVS 名前空間 `websocket`** に保存される
3. 音声チャネルを開くとき、その `url` / `token` / `version` を使って WebSocket 接続する
4. 本体が `hello` を送り、**サーバーの `hello` を 10 秒以内に受け取れないと `SERVER_TIMEOUT`** で失敗する
5. 以後、テキストフレームで JSON メッセージ、バイナリフレームで Opus 音声を双方向にやり取りする

## 設定値の入れ方

| NVS | キー | 内容 |
|---|---|---|
| `wifi` | `ota_url` | OTA エンドポイント。**ここを書けばファーム再ビルド不要で差し替えられる** |
| `websocket` | `url` | WebSocket の接続先。OTA 応答の `websocket.url` が保存される |
| `websocket` | `token` | 認証トークン。スペースを含まない場合は自動で `Bearer ` が前置される |
| `websocket` | `version` | バイナリプロトコル版（1 / 2 / 3）。0 なら既定の 1 |

OTA 応答の `websocket` オブジェクトは、**文字列・数値のメンバーがそのまま NVS へ書かれる**実装です。

## 接続時のリクエストヘッダ

- `Authorization`: `Bearer <token>`
- `Protocol-Version`: hello の `version` と同じ値
- `Device-Id`: **本体の MAC アドレス**
- `Client-Id`: ソフトウェア生成の UUID（NVS 消去や完全な再書き込みでリセットされる）

## hello の交換

### 本体 → サーバー

```json
{
  "type": "hello",
  "version": 1,
  "features": { "mcp": true },
  "transport": "websocket",
  "audio_params": {
    "format": "opus",
    "sample_rate": 16000,
    "channels": 1,
    "frame_duration": 60
  }
}
```

`features.aec` はサーバー側 AEC を使うビルドでのみ付きます。`frame_duration` は `OPUS_FRAME_DURATION_MS`（通常 60ms）。

### サーバー → 本体（必須）

```json
{
  "type": "hello",
  "transport": "websocket",
  "session_id": "任意の文字列",
  "audio_params": { "sample_rate": 24000, "frame_duration": 60 }
}
```

- **`transport` が `"websocket"` 以外だと拒否される**（実装で厳密に比較している）
- `session_id` は任意。受け取ると本体が記録し、以後のメッセージに含める
- `audio_params` は**サーバーが送る音声のパラメータ**。音楽再生の品質を上げるため下り 24000 を使う場合があると仕様書に記載
- **10 秒以内に返すこと**

## バイナリプロトコル（音声フレーム）

`websocket/version` で選択します。

### version 1（既定）

Opus データをそのままバイナリフレームで送る。メタデータなし。

### version 2

```c
struct BinaryProtocol2 {
    uint16_t version;       // プロトコル版
    uint16_t type;          // 0: OPUS, 1: JSON
    uint32_t reserved;
    uint32_t timestamp;     // ミリ秒。サーバー側 AEC 用
    uint32_t payload_size;
    uint8_t  payload[];
};
```

数値は**ネットワークバイトオーダー**（実装で `htons` / `ntohs` を使用）。サーバー側 AEC を使う構成向け。

### version 3

```c
struct BinaryProtocol3 {
    uint8_t  type;
    uint8_t  reserved;
    // 以降 payload
};
```

## 音声のパラメータ

- コーデックは **Opus**
- 上り（本体 → サーバー）は **16000Hz・モノラル**、フレーム長は通常 **60ms**
- 下り（サーバー → 本体）は hello の `audio_params` でサーバーが指定する（24000Hz を使う例が仕様書に記載）
- テキストフレームは JSON、バイナリフレームは Opus として扱われる（`binary` フラグで判別）

## JSON メッセージ

### 本体 → サーバー

| type | 内容 |
|---|---|
| `hello` | 上記の初期ハンドシェイク |
| `listen` | 録音の開始・停止。`state` = `start` / `stop` / `detect`（ウェイクワード検出）、`mode` = `auto` / `manual` / `realtime` |
| `abort` | TTS 再生や音声チャネルの中断。`reason` に `wake_word_detected` 等 |
| `mcp` | デバイス機能の公開とツール呼び出しの結果。payload は **JSON-RPC 2.0**（上流 `docs/mcp-protocol.md` 参照） |

ウェイクワード検出時は、`listen`/`detect` の前に**ウェイクワードの Opus 音声を先に送る**ことができ、サーバー側で声紋照合に使えると記載されています。

### サーバー → 本体

| type | 内容 |
|---|---|
| `hello` | 上記の応答（必須） |
| `stt` | 音声認識結果。`{"type":"stt","text":"..."}`。本体が画面に表示する |
| `llm` | 表情の指示。`{"type":"llm","emotion":"happy","text":"😀"}` |
| `tts` | `state` = `start`（これから音声を送る＝speaking へ遷移）/ `stop`（終了）/ `sentence_start`（`text` に読み上げ中の文を渡すと画面に出る） |
| `mcp` | ツール呼び出し（`tools/call`）。payload は JSON-RPC 2.0 |
| `system` | システム制御。リモート更新等に使われる |

## MCP でロボットを動かせる

本体は hello で `features.mcp: true` を宣言し、起動時に次の MCP ツールを登録します（実機の起動ログで確認）。

```
self.robot.get_head_angles / set_head_angles / set_led_color
self.robot.create_reminder / get_reminders / stop_reminder
```

つまり**自前サーバーからも `type: "mcp"` の `tools/call` で首を振らせたり LED を変えたりできる**ということです。「左に頭を向けて」が動く仕組みがこれです。

## 自前サーバーの最小要件

音声対話を成立させるだけなら、次を満たせば足ります。

1. **OTA エンドポイント（HTTP）**
   - `firmware`: 現在版と同じ `version` を返す（更新を走らせない）
   - `websocket`: `{"url": "ws://<host>/...", "token": "..."}`
   - `activation` は省略する
2. **WebSocket サーバー**
   - 接続後 10 秒以内に `hello`（`transport: "websocket"` 必須）を返す
   - バイナリフレームで届く Opus（16kHz mono / 60ms）を受けて音声認識する
   - 応答を `tts` の `start` → Opus バイナリ送信 → `stop` の順で返す
   - 必要に応じて `stt` / `llm` を送って画面表示と表情を動かす
3. **未検証**: 実際に上記だけで会話が成立するかは試していません。特に Opus のフレーム境界と `frame_duration` の整合、`version 1` での運用可否は実機で確認が必要です

## 参考

- 上流の公式仕様: [docs/websocket.md](https://github.com/78/xiaozhi-esp32/blob/v2.2.4/docs/websocket.md)（中国語、495 行）
- MCP プロトコル: [docs/mcp-protocol.md](https://github.com/78/xiaozhi-esp32/blob/v2.2.4/docs/mcp-protocol.md)
- 実装: `main/protocols/websocket_protocol.cc` / `main/ota.cc`
- 差し替えの全体設計は [voice-backend-plan.md](voice-backend-plan.md)
