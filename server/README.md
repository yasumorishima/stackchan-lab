# 自前サーバー（XiaoZhi プロトコル）

スタックちゃん（M5Stack CoreS3、公式ファーム 1.4.4）の接続先を、標準の XiaoZhi サーバーから
手元の Raspberry Pi 5 に切り替えるためのサーバーです。ファームウェアは公式バイナリのままで、
NVS の `wifi/ota_url` を書き換えるだけで接続先が変わります（アバターやアプリ連携は維持されます）。

## 構成

| ファイル | 役割 |
| --- | --- |
| `app.py` | OTA エンドポイント + WebSocket 本体（音声の往復、ツールの振り分け、会話履歴） |
| `opus_codec.py` | libopus の ctypes 束縛（上り 16kHz / 下り 24kHz / 60ms） |
| `local_stt.py` | ローカル音声認識（sherpa-onnx / Vosk / faster-whisper） |
| `mcp_client.py` | 本体の MCP サーバーに対するクライアント（initialize / tools/list / tools/call） |
| `server_tools.py` | サーバー側のツール（天気）。本体のツールと 1 つにまとめて LLM へ渡す |
| `places.py` | 地名 → 座標の表（`gen_places.py` が Open-Meteo geocoding から生成） |
| `gen_places.py` | `places.py` の生成器（座標を手書きしないための道具） |
| `test_client.py` | 本体を模した試験クライアント（MCP デバイス役も兼ねる） |
| `test_tools.py` | サーバー側ツールの単体試験（本体も LLM も不要） |
| `mock_llm.py` | さくらの AI Engine の代役（OpenAI 互換 + VOICEVOX 形式 TTS の中継） |
| `bench_stt.py` / `bench_stt_opus.py` / `bench_stt_cer.py` | 音声認識の速度・精度の測定 |
| `bench_tts.py` | VOICEVOX の合成速度の測定 |
| `server.conf.example` | 設定の雛形（実ファイルは `server.conf`、秘密はそこだけに置く） |
| `stackchan-server.service` | systemd ユニット |

## ツール

LLM には本体のツールとサーバー側のツールを 1 つの配列にまとめて渡し、
呼ばれた名前で振り分けます（`app.py` の `call_tool`）。

- 本体側（MCP 経由）: 機体の操作。ファーム 1.4.4 では `self.audio_speaker.set_volume`
- サーバー側: `get_weather`（場所・今 / 今日 / 明日 / 明後日）

天気の取得先は Open-Meteo です（API キー不要）。地名は同梱の表で引きます。
Open-Meteo の geocoding は日本語名を引けない（「札幌」は 0 件、"Sapporo" なら当たる）ため、
ローマ字で引いて返ってきた日本語名と座標を `gen_places.py` で表に落としてあります。

現在時刻は毎回システムプロンプトに差し込みます。LLM は今が何時か知らないので、
ツールにするより確実で、往復も増えません。

## 会話の続き

本体は一区切りごとに WebSocket を切ります。接続の中だけで履歴を持つと毎回はじめましての
会話になるので、`Device-Id` ごとにサーバー側で保持します（既定 30 分・直近 10 往復、
`HISTORY_TTL` / `HISTORY_TURNS`）。

## 動かし方

```bash
python3 -m venv .venv
./.venv/bin/pip install aiohttp sherpa-onnx
cp server.conf.example server.conf && chmod 600 server.conf
./.venv/bin/python app.py
```

読み上げには VOICEVOX エンジンが要ります。

```bash
docker run -d --name voicevox --restart unless-stopped -p 127.0.0.1:50021:50021 \
  voicevox/voicevox_engine:cpu-arm64-0.25.2
```

音声認識のモデル（sherpa-onnx / ReazonSpeech k2 v2 の int8）は
[csukuangfj/reazonspeech-k2-v2](https://huggingface.co/csukuangfj/reazonspeech-k2-v2) から
`encoder / decoder / joiner` の `int8.onnx` 3 つと `tokens.txt` を
`~/models/reazonspeech-k2-v2` に置きます（合計 160MB 程度）。

## 検証

実機なしで確認できます。

```bash
# サーバー側ツールだけ（本体も LLM も要らない）
./.venv/bin/python test_tools.py

# 素の往復（合成音声を Opus で送って、認識・応答・読み上げまで）
TEST_TEXT="今日の天気を教えて。" ./.venv/bin/python test_client.py

# 応答生成とツール呼び出しまで（モックを使うのでトークン不要）
./.venv/bin/python mock_llm.py &
SAKURA_BASE=http://127.0.0.1:8100 SAKURA_TOKEN=dummy LLM_BACKEND=sakura ./.venv/bin/python app.py
```

## 覚え書き

- 発話の処理は受信ループとは別のタスクで走らせています。受信ループの中で待つと、
  ツール呼び出しの応答を読めないまま自分の待ちでタイムアウトします。
- Open-Meteo は無料の公開 API なので 503（過負荷）が普通に返ります。読み上げる相手に
  生のエラーを聞かせないよう、3 回まで粘ってから最後に成功した値へ退避します
  （15 分以上古ければ「※N分前の情報」と添えます）。
- 設定ファイルを `.env` という名前にしていないのは、手元の環境で
  資格情報保護のフックが `.env` を含むコマンドを止めるためです。
- `server.conf` は追跡しません。トークンはここだけに置きます。

## 実物の LLM で通した記録（2026-07-29）

これまでの検証はモックの LLM（こちらが仕込んだ tool_calls を返すだけ）だったので、
本物のモデルでツール選択が成り立つかは分かっていませんでした。手元の Raspberry Pi 5 に
Ollama で `qwen2.5:3b` を置き、`SAKURA_BASE` をそこへ向けて OpenAI 互換の経路を
そのまま実モデルで駆動しました（本番の接続先は変えていません）。

```bash
# ツール選択だけを切り出して測る（音声経路は通さない）
SAKURA_BASE=http://127.0.0.1:11434 SAKURA_MODEL=qwen2.5:3b ./.venv/bin/python probe_llm.py

# 音声込みで一通り（別ポートで立てるので常駐中のサーバーは止まりません）
./e2e_real_llm.sh
```

分かったことと、それに合わせて直したところ:

- **システム文に現在時刻を入れるとプロンプトキャッシュが毎回捨てられる。**
  tools はチャットテンプレート上システム文の後ろに描画されるので、時刻が分単位で
  変わると tools 約 250 トークンごと作り直しになります。同じ発話列を A/B で測って
  1 往復 **35.1 秒 対 7.8 秒**（再利用トークン 96 対 360）でした。時刻はシステム文から
  外し、`stamped_user()` で発話の先頭に添えています。再現用は `probe_cache.py`。
- **ツールの往復を履歴に残さないと、省略形の追い質問でツールを呼び直さない。**
  「あしたの大阪の天気」に答えた直後の「じゃあ鳥取はどう？」で、最終文だけを履歴に
  持っていたときは数値を作って答えました（実測値と全く違う値を読み上げた）。
  `respond()` が途中の `tool_calls` と結果も返すようにして履歴へ残したら、
  ツールを呼び直すようになりました。切り詰めで `tool` が孤立すると API が弾くので、
  `trim_history()` は必ず `user` から始まるように削ります。
- **生成が上限で切れるとツール呼び出しが壊れ、`</tool_call>` が本文として読み上げられる。**
  `clean_reply()` で閉じタグとツール呼び出しらしい JSON を落とし、残らなければ
  素直に言い直しをお願いします。単体は `test_clean.py`。
- 最初の 1 発話が遅いのはモデルではなくキャッシュが空だからです。`warm_llm.py` が
  本番と同じ「システム文 + tools」でひと往復して接頭辞を載せます。

残っているのはモデル側の粗さで、サーバーの問題ではありません（3B での観測）。
追い質問で `when` を落として今日の天気を答える、2 文以内の指示を超える、といった具合です。
