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
| `test_gateway.py` | 既定経路の読み取りの単体試験（経路表を差し替えるので回線に依存しない） |
| `test_hello_grace.py` | 本体の hello が届かない時にサーバーから先に hello を出すかの確認 |
| `test_firmware_handshake.py` | ファームの WebSocket 握手を byte 単位で写して通ることを確認する |
| `test_broken_mcp.py` | 壊れた mcp メッセージ 1 通で対話ごと落ちないことの確認 |
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

## 小さいモデルでも破綻させない（2026-07-30）

前回「残りはモデル側の粗さ」と書いた 2 点を実際に測ったところ、**サーバー側で直せる**
ことが分かりました。temperature 0.7 なので 1 回では当たり外れが読めません。
`probe_followup.py` で各ケース 3 回ずつ流して回数で見ます。

```bash
SAKURA_MODEL=qwen2.5:3b REPEAT=3 ./.venv/bin/python probe_followup.py
```

- **`when` をモデルに催促すると、かえってツールを呼ばなくなる。**
  「あしたの大阪」の直後の「じゃあ鳥取はどう？」で `when` が落ち、既定の today で
  埋まって**今日の天気を明日だと思って聞いている人に答える**状態でした。JSON schema の
  `required` に入れると、3B は日を決められない時に**ツール自体を呼ぶのをやめて数値を
  作り話**するようになりました（呼び出し 1/9）。説明文で「必ず入れる」と促しても同じです。
  そこで催促はやめ、落ちた時に**サーバーが決める**ようにしました（`infer_when`）。
  順に ①発話に日を表す語があればそれ ②少し前に調べた日を引き継ぐ ③どちらも無ければ today。
- **引き継ぎには時限を付ける。** 何十分も前の「明日」を新しい質問に継ぐと誤答になるので、
  `WHEN_TTL`（既定 180 秒）以内だけ引き継ぎます。本体は 1 発話ごとに切断するので、
  接続内の状態ではなく Device-Id ごとに覚えます（`when_store`）。
- **日を表す語は裸の 1 文字を拾わない。** 「今」「いま」を入れていたら
  「今週の天気は？」「今度の日曜は？」「大阪に住んで**いま**すが天気は？」が全部
  現在の実況になりました。「今の」「現在」等のはっきりした言い方だけ拾います。
- **過去は「分からない」と言う。** 予報しか取れないので、「昨日の天気」に今日の天気を
  答えないようにしました（黙って別の日を答えるのが一番まずい）。
- **「2 文以内」等の形はシステム文で頼まず code 側で切る。** システム文を足すほど
  ツール選択が落ちます（3B・各 3 回、同じ probe）。

  | システム文 | ツール呼び出し | 2 文以内 |
  |---|---|---|
  | 指示なし | 7/9 | 3/9 |
  | 7/29 までの文 | 4/9 | 6/9 |
  | 指示を足した長い文 | 1/9 | 8/9 |
  | **短縮 + code で切る** | **7/9** | **読み上げ 9/9** |

  システム文はモデルにしかできないこと（ツールを使う・作り話をしない）だけに絞り、
  文数と長さは `shorten_reply()` で切ります。`probe_llm.py` の 7/7 は維持しています。
  数える用の `plain_sentences()` は読み上げ用の `split_sentences()` とは別物です
  （後者は 8 字未満の断片を前の文へくっつけるので、文数には使えません）。
- **発話に添えた時刻をそのまま読み上げてくる。** 「どういたしまして。（2026年7月31日(金)
  7時00分)」のように書き写します（しかも日付を 1 日間違えていました）。`clean_reply()`
  で落とします。箇条書きの記号と改行も声には出ないので落としますが、**行頭の
  マイナスは気温の符号かもしれない**ので記号のあとに空白が続く時だけ剥がします
  （`-3度です` はそのまま）。

## ネットが切れても話せるようにする（2026-07-30）

STT（sherpa-onnx）と TTS（VOICEVOX）はもともとローカルなので、LLM だけ用意すれば
家の中だけで会話が成立します。本番（さくらの AI Engine）が使えない時に、手元の
小さいモデルへ落ちるようにしました。

- 落ちる条件は「向こう側の都合」だけです。401/403/429/5xx・接続不能・timeout では
  落ちますが、**400/404/422 では落ちません**（こちらの組み立てが悪いので、投げ直しても
  同じように失敗するだけです）。
- **落ちている間は本番を叩き直しません。** 1 発話でツール往復ぶん 2 回叩くので、
  毎回 timeout を待つと本体が何十秒も黙ります。一度失敗したら `PRIMARY_COOLDOWN`
  （既定 120 秒）はローカルへ直行し、復帰したらログに残して本番へ戻します。
- 実推論を使わずに確かめられます（`test_fallback.py`）。その場に小さな OpenAI 互換
  サーバーを立てて「ローカル側」の役をさせるので、Ollama も本体も要りません。
  音声込みで確かめるときは `e2e_offline.sh`（本番の宛先を落として fallback を通す）。

```
FALLBACK_LLM_BASE=http://127.0.0.1:11434
FALLBACK_LLM_MODEL=qwen2.5:3b
PRIMARY_LLM_TIMEOUT=30      # 本番を待つ上限。空にはできない
PRIMARY_COOLDOWN=120        # 失敗後、本番を試さない時間
```

**モデルを大きくしても解決しません。** 同じ probe を `qwen2.5:7b` で流したら 3B より
悪く（追い質問 2/9 対 5/9）、意味のない英単語を混ぜてきました。加えて 8GB の
Raspberry Pi 5 では swap が満杯になり load average 56 まで上がって、SSH が
handshake を返せなくなりました（機体は動いたままです）。**この箱に置くのは 3B までです。**
