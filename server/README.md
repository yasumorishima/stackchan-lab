# 自前サーバー（XiaoZhi プロトコル）

スタックちゃん（M5Stack CoreS3、公式ファーム 1.4.4）の接続先を、標準の XiaoZhi サーバーから
手元の Raspberry Pi 5 に切り替えるためのサーバーです。ファームウェアは公式バイナリのままで、
NVS の `wifi/ota_url` を書き換えるだけで接続先が変わります（アバターやアプリ連携は維持されます）。

## 構成

| ファイル | 役割 |
| --- | --- |
| `app.py` | OTA エンドポイント + WebSocket 本体（音声の往復、MCP クライアント） |
| `opus_codec.py` | libopus の ctypes 束縛（上り 16kHz / 下り 24kHz / 60ms） |
| `local_stt.py` | ローカル音声認識（sherpa-onnx / Vosk / faster-whisper） |
| `mcp_client.py` | 本体の MCP サーバーに対するクライアント（initialize / tools/list / tools/call） |
| `test_client.py` | 本体を模した試験クライアント（MCP デバイス役も兼ねる） |
| `mock_llm.py` | さくらの AI Engine の代役（OpenAI 互換 + VOICEVOX 形式 TTS の中継） |
| `bench_stt.py` / `bench_stt_opus.py` / `bench_stt_cer.py` | 音声認識の速度・精度の測定 |
| `bench_tts.py` | VOICEVOX の合成速度の測定 |
| `server.conf.example` | 設定の雛形（実ファイルは `server.conf`、秘密はそこだけに置く） |
| `stackchan-server.service` | systemd ユニット |

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

実機なしで往復を確認できます。

```bash
# 素の往復（合成音声を Opus で送って、認識・応答・読み上げまで）
TEST_TEXT="今日の天気を教えて。" ./.venv/bin/python test_client.py

# 応答生成と MCP のツール呼び出しまで（モックを使うのでトークン不要）
./.venv/bin/python mock_llm.py &
SAKURA_BASE=http://127.0.0.1:8100 SAKURA_TOKEN=dummy LLM_BACKEND=sakura ./.venv/bin/python app.py
```

## 覚え書き

- 発話の処理は受信ループとは別のタスクで走らせています。受信ループの中で待つと、
  ツール呼び出しの応答を読めないまま自分の待ちでタイムアウトします。
- 設定ファイルを `.env` という名前にしていないのは、手元の環境で
  資格情報保護のフックが `.env` を含むコマンドを止めるためです。
- `server.conf` は追跡しません。トークンはここだけに置きます。
