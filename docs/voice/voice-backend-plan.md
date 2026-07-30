# 音声バックエンドを差し替える検討メモ

出荷時の構成では、音声認識・LLM・読み上げのすべてを中国のクラウドサービス **XiaoZhi（小智）** が処理します。会話内容が外部サーバーへ送られる前提の構成なので、自分の環境で完結させる／国内サービスに寄せる方向を検討します。

**2026-07-25 時点の調査記録。** 実装を読んで確認した事実と、未検証の想定を分けて書いています。**アバター・MCP ツール・本体 UI は維持する**方針です。

## 結論から: ファームウェアの再ビルドは不要

当初は「`CONFIG_OTA_URL` を変えるために自前ビルドが必要、しかし自前ビルドは公式アプリとペアリングできない」という詰みを想定していました。実装を読んだ結果、**再ビルドせずに接続先を変更できる**ことが分かりました。

xiaozhi-esp32 の `Ota::GetCheckVersionUrl()`（v2.2.4）が根拠です。

```cpp
std::string Ota::GetCheckVersionUrl() {
    Settings settings("wifi", false);
    std::string url = settings.GetString("ota_url");
    if (url.empty()) {
        url = CONFIG_OTA_URL;
    }
    return url;
}
```

**NVS の `wifi` 名前空間にある `ota_url` が優先され、空のときだけビルド時の `CONFIG_OTA_URL` にフォールバック**します。したがって NVS に `ota_url` を書き込めば、**公式バイナリのまま**接続先を自前サーバーへ向けられます。

## ファームウェアの構成（確認済み）

`firmware/repos.json` により、上流の **[78/xiaozhi-esp32](https://github.com/78/xiaozhi-esp32) の `v2.2.4`** を取得し、`patches/xiaozhi-esp32.patch` を適用する構成です。したがって音声パイプラインのプロトコルは xiaozhi-esp32 v2.2.4 の実装がそのまま基準になります。M5Stack 側のコード（`firmware/main/`）には `hal/`・`apps/`・`stackchan/`（アバターやモーション）が置かれています。

接続先の設定は用途ごとに分離しています（`firmware/main/Kconfig.projbuild`）。

| 設定 | 既定値 | 役割 |
|---|---|---|
| `CONFIG_STACKCHAN_SERVER_URL` | `http://47.113.125.164:12800` | StackChan バックエンド。`/stackChan/device/user`・`/stackChan/apps`・`/stackChan/ws` を組み立てる。help に「self-hosted deployment に向けるには `sdkconfig.defaults.local` で上書きせよ」と明記 |
| `CONFIG_OTA_URL` | `https://api.tenclass.net/xiaozhi/ota/` | XiaoZhi 側の OTA / サーバー情報取得先。**音声パイプラインの接続先はこちら**。NVS `wifi/ota_url` で上書き可能 |

リポジトリには **`server/` として公式バックエンドの完全なソース**も含まれています（Go + MySQL スキーマ + Flutter Web 管理画面 + Docker / Kustomize + デバイスとアプリ間の WebSocket 中継 + XiaoZhi 連携）。アプリ連携機能まで自前化したい場合はこちらも自己ホストできます。

## OTA エンドポイントが返すべき応答（確認済み）

`main/ota.cc`（v2.2.4）は、`ota_url` に対して HTTP で問い合わせ、200 応答の JSON を次のように解釈します。

- **`firmware`**: `{"version": "...", "url": "..."}`。`version` が現在版より新しいと更新が走る。`force: 1` で強制。**更新させたくない場合は現在版と同じ version を返す**
- **`websocket`**: オブジェクトの各メンバー（文字列・数値）が **NVS 名前空間 `websocket` にそのまま保存**される。ここに音声用の `url` と `token` を入れる
- **`mqtt`**: 同様に NVS 名前空間 `mqtt` へ保存される（WebSocket 方式なら不要）
- **`activation`**: `message` / `code` / `challenge` / `timeout_ms`。アクティベーションコード表示のフロー。**自前サーバーでは省略できる**（省略すれば `has_activation_code_` は false のまま）
- 送信ヘッダは `Activation-Version`（シリアル有無で 1 or 2）、`Device-Id`（MAC アドレス）、`Accept-Language`、`Content-Type: application/json`
- アクティベーションを使う場合、`ota_url` の末尾に `/activate` を付けたエンドポイントも呼ばれる

## 想定手順（未実施）

1. **NVS イメージを生成する**。ESP-IDF の `nvs_partition_gen.py`（単体の Python スクリプトとして取得可）に CSV を渡し、`wifi` 名前空間に `ota_url = http://<自前ホスト>/xiaozhi/ota/` を持つ 16KB のイメージを作る
2. **`esptool` で `0x9000` に書き込む**（パーティションテーブル上の `nvs` は offset `0x9000` / size `0x4000`）。この操作で既存の NVS（Wi-Fi 設定・アカウント紐付け）は消える
3. **公式アプリでペアリングをやり直す**。BLE 設定サーバーが受け付けるコマンドは `setWifi` / `getWifiStatus` / `handshake` の 3 つだけで、`setWifi` は同じ `wifi` 名前空間の ssid / password のみを書くため、**先に入れた `ota_url` は残る**
4. 本体で AI エージェントに入ると、自前の OTA エンドポイントへ問い合わせが飛ぶ。ここで `firmware`（現在版と同じ version）と `websocket`（自前サーバーの url / token）を返す
5. **サーバー側**を用意する。LLM は OpenAI 互換 API、読み上げは VOICEVOX、音声認識はローカル処理を想定

**この経路ならファームウェアは公式バイナリのままなので、アバター・MCP ツール・アプリ連携がすべて維持されます。**

## 無償枠の制約（外部 API を使う場合）

候補として「さくらのAI Engine」（OpenAI / Anthropic 互換 API）を調べました。2026-07 のキャンペーン告知に記載された無償枠は次のとおりです。

| 用途 | 無償枠 |
|---|---|
| Chat completions（テキスト応答生成） | 月 **3,000** リクエスト |
| Audio transcriptions（音声認識） | 月 **50** リクエスト |
| Audio speeches（読み上げ） | 月 **50** リクエスト |

自動課金は発生しない仕様と案内されています。**音声系が月 50** なので、1 往復で認識 1 + 読み上げ 1 を消費する音声対話では**月 25 往復程度**しか使えません。したがって、

- **音声パイプラインの丸ごと置き換えには無償枠が不足する**
- **LLM（頭脳）としてなら 3,000 リクエストで十分**

という切り分けになります。読み上げは VOICEVOX（無償・自前ホスト）、音声認識はローカルに寄せるのが現実的です。無償枠の値は変更されうるので、着手時に最新の記載を確認します。

## 未検証・要注意

- **自前 OTA / WebSocket サーバーが xiaozhi のプロトコルに実際に適合するかは未検証**。応答スキーマは読み取れたが、WebSocket 側の音声フレーム仕様（`hal_ws_avatar.cpp` および上流の protocol 実装）はまだ読んでいない
- **NVS を書き換えるとアカウント紐付けが消える**ため、ペアリングのやり直しが必要。手順 3 が通ることは未検証（公式ファームのままなので通る見込み）
- `nvs_partition_gen.py` で生成したイメージが、この版の NVS フォーマットとして正しく読まれるかは未検証
- **公式版に戻す手段は確立済み**（USB で公式バイナリを書き戻す。手順は [../setup/firmware-flash.md](../setup/firmware-flash.md)）

## 代替案: コミュニティ製ファーム（採らない）

`AI_StackChan_Ex`（`ronron-gh/AI_StackChan_Ex`）系は SD カード上の YAML で API キーやサービスを切り替えられ、OpenAI 互換構成や VOICEVOX に対応しています。ただし、

- 歴史的に **SG90 の PWM サーボ前提**。本機は **FEETECH SCS0009 のシリアルサーボ**なので `servo_type` を `SCS`、センター 150/150、UART を GPIO6/7 に設定する必要がある
- **公式アプリの機能（アバター・MCP ツール群・リモコン連携）は失われる**

アバターを維持したいので**この案は採りません**。

## 現状

**2026-07-26: 自前サーバーの実装と、実機を使わない検証まで完了。** 実装は手元の Linux 機（Raspberry Pi 5 / aarch64）で動かしています。

確認できたこと（本体を模した試験クライアントによる実測）:

- OTA エンドポイントが現在版と同じ `version` を返して更新を走らせない
- サーバーの `hello` を本体が受け取れる形（`transport` は `websocket` 厳密一致）で返せる
- 上り Opus（16000Hz mono / 60ms）を復号できる
- `stt` / `llm` / `tts`(start → sentence_start → stop) を送り、下り Opus（24000Hz）を生成できる

本体の接続先を切り替える NVS イメージ（`wifi` 名前空間に `ota_url`、16384 bytes）も生成済みです。**書き込みは未実施**で、書くと既存の Wi-Fi 設定とアプリの紐付けが消えるため再ペアリングが必要になります。

**2026-07-30: 応答生成の差し替えまで含めて実装済み。残るのはトークンだけ。** 本番はさくらの AI Engine のままですが、**使えない時は手元の小さいモデルへ落ちる**ようにしました。認識と合成はもともとローカルなので、これで**ネットが切れても会話が成立**します。

- 落ちる条件は向こう側の都合だけ（401/403/429/5xx・接続不能・待ち時間切れ）。こちらの組み立てが悪い 400/404/422 では落ちません
- 落ちている間は本番を叩き直しません。1 発話で道具の往復ぶん 2 回叩くので、毎回待つと本体が数十秒黙ります
- 実物の推論を使わずに確かめられます（`server/test_fallback.py`）。音声込みは `server/e2e_offline.sh`

**本体だけで会話を完結させることはできません。** CoreS3 は Flash 16MB / PSRAM 8MB で、いま使っている 3B（4bit で約 1.9GB）に対して 2 桁以上足りません。本体単体でネットなしにできるのは、Espressif の ESP-SR による起動語の検出と**決まった命令の認識（最大 200 個・日本語非対応）**までです。応答生成を外に置く構成は、この機体では前提です。

### 無償枠の値（2026-07-26 に公式マニュアルで確認）

上の「無償枠の制約」はキャンペーン告知を元にした記述でしたが、公式マニュアルで次を確認しました。

| 用途 | 無償枠 |
|---|---|
| チャット補完 (chat completions) | 月 3,000 リクエスト |
| ベクトル埋め込み (embeddings) | 月 10,000 リクエスト |
| 音声の文字起こし (audio transcriptions) | 月 50 リクエスト |
| 音声合成 (audio speech) | 月 50 リクエスト |

- API のベースは `https://api.ai.sakura.ad.jp`。OpenAI 互換の `/v1/chat/completions`、Anthropic 互換の `/v1/messages` があります
- 読み上げには **VOICEVOX 形式のエンドポイント**（`/tts/v1/audio_query`・`/tts/v1/synthesis`）も用意されています。ただし月 50 リクエストなので、常用するなら VOICEVOX を自分で動かすほうが現実的です
- 無償枠を超えても課金されず、レートリミットがかかる仕様と記載されています
- **利用開始には電話認証とクレジットカードの登録が必要**です（無償プランの場合も）

### 2026-07-27: 音声認識と読み上げをローカルで確定

無償枠が音声だけ月 50 リクエストと厳しいので、**読み上げと音声認識は Raspberry Pi 5 上のローカル処理に寄せました**。外部 API に残るのは応答生成（チャット補完、月 3,000）だけです。

- 音声認識 = sherpa-onnx + ReazonSpeech k2 v2（zipformer transducer、int8。モデルは約 160MB）
- 読み上げ = ローカルの VOICEVOX エンジン（Docker の `cpu-arm64` イメージ、ずんだもん）
- 常駐は systemd。設定は EnvironmentFile 経由で、トークンはリポジトリに置きません

認識バックエンドは、VOICEVOX で作った 12 文を本体と同じ Opus（16000Hz / 60ms）に通してから認識させて選びました。

| バックエンド | CER | RTF | 備考 |
| --- | --- | --- | --- |
| sherpa-onnx + ReazonSpeech k2 v2 (int8) | 4.3% | 0.16 | 誤りは「明日 → あした」のような表記差だけ |
| Vosk small-ja 0.22 | 11.3% | 1.05 | 「温度 → 腕」のように意味が壊れる |
| faster-whisper small (int8) | 1.4% | 2.48 | 精度は最良だが 2.5 秒の発話に 6 秒かかる |

RTF は音声の長さに対する処理時間です。1.0 を超えると認識が発話に追いつかないので、会話で使えるのは sherpa-onnx だけでした。合成音声による測定なので、実マイクより楽観的な値です。

読み上げは文ごとに合成して送り、次の文は再生中に裏で作ります。最初の音が出るまで 2.2〜2.5 秒です。VOICEVOX のスレッド数は明示指定しないほうが速く、`VV_CPU_NUM_THREADS=4` はかえって遅くなりました（1.8 秒の音声の合成が 2.98 秒 → 4.25 秒）。

残っているのは、応答生成をさくらの AI Engine につなぐこと（アカウントトークンの発行待ち）と、実機での疎通確認です。
