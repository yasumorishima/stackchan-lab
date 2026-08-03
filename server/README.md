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
| `server_tools.py` | サーバー側のツール（天気・ドル円・株価指数・プロ野球ほか）。本体のツールと 1 つにまとめて LLM へ渡す |
| `places.py` | 地名 → 座標の表（`gen_places.py` が Open-Meteo geocoding から生成） |
| `gen_places.py` | `places.py` の生成器（座標を手書きしないための道具） |
| `test_client.py` | 本体を模した試験クライアント（MCP デバイス役も兼ねる） |
| `test_tools.py` | サーバー側ツールの単体試験（本体も LLM も不要） |
| `test_gateway.py` | 既定経路の読み取りの単体試験（経路表を差し替えるので回線に依存しない） |
| `test_hello_grace.py` | 本体の hello が届かない時にサーバーから先に hello を出すかの確認 |
| `test_firmware_handshake.py` | ファームの WebSocket 握手を byte 単位で写して通ることを確認する |
| `test_broken_mcp.py` | 壊れた mcp メッセージ 1 通で対話ごと落ちないことの確認 |
| `test_auto_mode.py` | 本体が `listen stop` を送らない `mode:auto` でも応答できるかの確認 |
| `mock_llm.py` | さくらの AI Engine の代役（OpenAI 互換 + VOICEVOX 形式 TTS の中継） |
| `bench_stt.py` / `bench_stt_opus.py` / `bench_stt_cer.py` | 音声認識の速度・精度の測定 |
| `bench_tts.py` | VOICEVOX の合成速度の測定 |
| `server.conf.example` | 設定の雛形（実ファイルは `server.conf`、秘密はそこだけに置く） |
| `stackchan-server.service` | systemd ユニット |

## ツール

LLM には本体のツールとサーバー側のツールを 1 つの配列にまとめて渡し、
呼ばれた名前で振り分けます（`app.py` の `call_tool`）。

- 本体側（MCP 経由）: 機体の操作。ファーム 1.4.4 では `self.audio_speaker.set_volume`
- サーバー側: `get_weather`（場所・今 / 今日 / 明日 / 明後日 / これから1週間）/ `get_usdjpy`（ドル円レート）/ `get_stock_index`（日経平均・ダウ・S&P500）/ `get_llm_quota`（さくら無料枠の使用回数と残り）/ `get_crypto`（ビットコイン・イーサリアムの円建て価格）/ `get_news`（NHK RSS の主要見出し）/ `get_quake`（気象庁の地震情報）/ `get_warning`（気象庁の警報・注意報、既定は神奈川県）/ `get_typhoon`（気象庁の台風情報）/ `get_heat`（環境省の暑さ指数と熱中症警戒アラート、既定は横浜）/ `get_train`（ODPT の運行情報。`ODPT_TOKEN` があれば京急・JR東・横浜市営地下鉄・東急・相鉄・東京メトロ、無ければ都営地下鉄のみ）/ `get_onthisday`（Wikipedia の今日は何の日）/ `get_sky`（月齢と日の出・日の入り、計算のみ）/ `get_fuel_surcharge`（国際線の燃油サーチャージ。行き先を言わなければ中東） / `get_travel_advisory`（外務省の海外安全情報。国ならその国、「中東」「ヨーロッパ」等ならその地域ぜんぶ、省略すれば世界ぜんぶを全 207 か国の集計で答える） / `get_baseball`（NPB の今日の試合速報と勝敗表の順位。球団を言わなければ全試合と両リーグの順位）/ `get_roster_move`（NPB の公示＝出場選手の登録・登録抹消）/ `get_cheer_song`（横浜DeNAベイスターズ公式の選手応援歌の歌詞。ふりがなの方を読む。歌詞はこのリポジトリに置かず実行時に取りに行く）

天気の取得先は Open-Meteo です（API キー不要）。地名は同梱の表で引きます。
ドル円は Yahoo Finance → Coinbase → open.er-api.com の順で引きます（すべてキー不要・無料）。
株価指数は同じ Yahoo Finance の chart API で引きます（query1 → query2 のホスト冗長 + 6 時間の stale cache）。取引時間外は「◯月◯日の終値」と断って読み上げます。
無料枠の残りは、利用量 API が無い（/v1/usage 等は 404・応答ヘッダにも情報なし）ため、このサーバーから送った成功リクエストを月次で自前カウントして答えます（サーバー外の消費は数えられない旨も読み上げます）。
暗号資産は CoinGecko（1 回で両方 + 24時間変動率）→ Yahoo Finance の順で引きます。Coinbase の spot は BTC-JPY が実勢の 3.6 倍の異常値を返した実測があるため使いません。
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

読み上げの既定は Open JTalk です（Raspberry Pi 5 の CPU でも 1 文 0.3 秒で合成できるため）。

```bash
sudo apt install open-jtalk open-jtalk-mecab-naist-jdic hts-voice-nitech-jp-atr503-m001
```

声質を優先するなら VOICEVOX も選べます（`TTS_BACKEND=voicevox`）。
合成は 1 文 2〜5 秒かかるので、初音までの待ちが数秒延びます。

```bash
docker run -d --name voicevox --restart unless-stopped -p 127.0.0.1:50021:50021 \
  voicevox/voicevox_engine:cpu-arm64-0.25.2
```

音声認識のモデル（sherpa-onnx / ReazonSpeech k2 v2 の int8）は
[csukuangfj/reazonspeech-k2-v2](https://huggingface.co/csukuangfj/reazonspeech-k2-v2) から
`encoder / decoder / joiner` の `int8.onnx` 3 つと `tokens.txt` を
`~/models/reazonspeech-k2-v2` に置きます（合計 160MB 程度）。

## 読み上げの速さ

Open JTalk は合成のたびに前後へ固定の無音（前 0.41 秒・後 0.58 秒ほど）を付ける。文ごとに合成するため短い返事ほど無音の比率が上がる（実測 27〜56%）ので、`_trim_silence` で前後を落とし、区切りに `OJT_END_PAUSE`（既定 0.12 秒）だけ足し直す。しきい値 `OJT_TRIM_LEVEL`（既定 60）はパディングの微小ノイズ（振幅 20〜50）より上、弱い子音（振幅 50〜100）より下に置いてある。

かたまりが長すぎる時の伸びは `split_long_runs` が別に見ている。一次の物差しは`_mora_est`（読みのモーラ数を合成せずに見積る）で、`OJT_MAX_MORA`（既定 30）で切る。

**ただし、伸びるかどうかは長さの閾値では決まらない**。2026-08-02 の統制実験で、無意味語「あさひ」を 10 回並べた読点なし 30 モーラは 0.122 秒/モーラで健全なのに、実文「何か聞きたいことややってほしいことがあれば教えてください」は 29 モーラで 0.235、「春のことでも他に気になることがあれば教えてくださいね。」は 0.358 まで落ちた。**同じ文に読点を 1 つ入れるだけで 0.127 に戻る**。壊れるかどうかは語の並びしだいで、モーラ数からも字数からも予測できない（以前ここに書いた「膝は 34 モーラ付近」は、この実験で否定された）。

そこで `_synth_checked` が**合成してから実測の速さを見る**。`open_jtalk -ot` の音素トレースから 秒/モーラ を出し、`OJT_BAD_PACE`（既定 0.17）を超えていたら、その片を 0.6 倍の長さに分け直して合成し直す（`OJT_RESPLIT_ROUNDS` 回まで、12 モーラ未満の片は対象外）。実測で 8.77→5.92 秒 / 10.60→4.46 秒 / 7.05→4.31 秒に縮み、**もともと健全だった 8 文は完全に不変**だった（閾値を下げる方式と違い、余計な分割が増えない）。実文 19 片の速さは 0.114〜0.155 で、誤って分け直された片は無い。

見積りは数字を読みの長さで数える（`3000` は 4 モーラ「さんぜん」、`2996` は 13、`64362` は 17）。全角の数字・英字・`％` は半角に直してから数える（合成器は同じに読む）。`007` のように 0 で始まる並びは1 桁ずつ読まれる。**見積りは実測 20 例すべてで実際以上**（比 1.00〜1.28）＝早めに切る安全側に倒してある。過小になると分割し損ねるため、下振れする変更は入れない。

**数字や英字の途中では切らない**（「29」を「2」「9」に割ると読みが壊れる）。語の途中でしか切れない場合は分割しない（間延びの方がまし）。

読み上げの途中で話しかけられるよう、`split_for_barge` が長い音を「いちばん静かな所」（`BARGE_PAUSE_RMS` 以下の 60ms）で切り、`listen_gap` がその継ぎ目で `tts stop` → 鳴り終わり待ち → `BARGE_WINDOW`（既定 0.4 秒）の聞き取り → `BARGE_SPEECH_MS`（既定 240ms）以上の声があれば残りを中止、を行う。`BARGE_MIN_SEC`（既定 6 秒）に満たない返事には窓を挟まない。`BARGE_IN=0` で無効。

`OJT_LOG_PACE=1` を立てると、合成のたびに「秒/モーラ」をログに出す（診断用）。健全なら 0.12〜0.14 に収まる。

## 検証

実機なしで確認できます。

```bash
# サーバー側ツールだけ（本体も LLM も要らない）
./.venv/bin/python test_tools.py

# 回線にも本体にも依存しないもの
./.venv/bin/python test_gateway.py            # 既定経路の読み取り
./.venv/bin/python test_firmware_handshake.py # ファームの握手を byte 単位で写して当てる
./.venv/bin/python test_hello_grace.py        # hello が来ない時にこちらから先に出すか
./.venv/bin/python test_broken_mcp.py         # 壊れた mcp 1 通で落ちないか

# 素の往復（合成音声を Opus で送って、認識・応答・読み上げまで）
TEST_TEXT="今日の天気を教えて。" ./.venv/bin/python test_client.py

# 応答生成とツール呼び出しまで（モックを使うのでトークン不要）
./.venv/bin/python mock_llm.py &
SAKURA_BASE=http://127.0.0.1:8100 SAKURA_TOKEN=dummy LLM_BACKEND=sakura ./.venv/bin/python app.py
```

## 本体が繋がるのに黙って切れる時（2026-07-30）

本体は接続してくるのに、こちらの受信ログが 0 行のまま 15 秒ほどで切れることがあります。
ファーム（上流 xiaozhi-esp32 v2.2.4）の待ち時間は 2 段です。

1. 握手要求を送り、完了を 10 秒待つ（判定は応答に `HTTP/1.1 101` を含むかだけ）
2. 成功して初めて `hello` を送り、サーバーの `hello` をさらに 10 秒待つ

**1 で切れた場合、`hello` は 1 バイトも出ません。** 受信 0 行は「本体が送っていない」
場合と「送ったが届いていない」場合の両方でそう見えます。切り分けの順に見てください。

- **まずファイアウォール**。本体は同じ LAN から来ます。待ち受けだけ合っていても
  経路で落ちていれば繋がりません（`ufw` が既定拒否なら 8000 番を LAN 限定で開ける）
- **握手が本体の形で通るか**は `test_firmware_handshake.py` で確認できます。
  ファームの WebSocket クライアントの要求を byte 単位で写してあります
- **`hello` の往路が落ちている場合の保険**として、`HELLO_GRACE`（既定 3 秒）待って
  本体の `hello` が来なければ、サーバーから先に `hello` を送ります。ファーム側は
  `hello` の順序を問わないので先に送っても壊れません。発動するとログに
  「hello が 3.0 秒来ないので server hello を先に送る」と残るので、
  **この行が出ていれば往路が落ちていた証拠**になります

## 覚え書き

- 発話の処理は受信ループとは別のタスクで走らせています。受信ループの中で待つと、
  ツール呼び出しの応答を読めないまま自分の待ちでタイムアウトします。
- Open-Meteo は無料の公開 API なので 503（過負荷）が普通に返ります。読み上げる相手に
  生のエラーを聞かせないよう、3 回まで粘ってから最後に成功した値へ退避します
  （15 分以上古ければ「※N分前の情報」と添えます）。
- 設定ファイルを `.env` という名前にしていないのは、手元の環境で
  資格情報保護のフックが `.env` を含むコマンドを止めるためです。
- `server.conf` は追跡しません。トークンはここだけに置きます。
- 受信ループは `try/finally` で囲ってあります。後始末で libopus の decoder/encoder を
  解放しており、ここを飛ばすとネイティブ側が接続ごとに残るためです。

## 作った当時の記録

実物の応答生成で通すまでの測定、小さいモデルで破綻させないために決めたこと、回線が切れた時の扱いは [docs/server-history.md](../docs/server-history.md) にあります。
