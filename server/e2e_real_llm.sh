#!/usr/bin/env bash
# 実 LLM（Ollama）を繋いだ状態で、音声経路まで含めた E2E を通す。
# 本番の systemd（8000番・llm=dry）は止めずに、8001番で別インスタンスを立てる。
set -u
cd /home/yasu/stackchan-server
set -a
. ./server.conf
set +a
export PORT=8001
export PUBLIC_HOST=127.0.0.1
export LLM_BACKEND=sakura
export SAKURA_BASE=http://127.0.0.1:11434
export SAKURA_MODEL=qwen2.5:3b
export SAKURA_TOKEN=dummy
export STT_BACKEND=sherpa
export TTS_BACKEND=voicevox

LOG=/home/yasu/stackchan-server/e2e_real_llm.log
: > "$LOG"
fuser -k 8001/tcp 2>/dev/null || true
sleep 1

./.venv/bin/python app.py >> "$LOG" 2>&1 &
SRV=$!
trap 'kill $SRV 2>/dev/null' EXIT
sleep 12
echo "== server 8001 起動 =="
grep -m1 backends "$LOG" || true

./.venv/bin/python warm_llm.py
echo "== モデル warm 済み =="

run() {
  echo ""
  echo "######## $1"
  OTA_URL=http://127.0.0.1:8001/xiaozhi/ota/ TEST_TEXT="$1" \
    timeout 240 ./.venv/bin/python test_client.py 2>&1 \
    | grep -E "upstream:|\"type\": \"stt\"|\[device\]|sentence_start|RESULT|Traceback|Error"
}

run "あしたの大阪の天気を教えて。"
run "じゃあ鳥取はどう？"
run "音量を五十にして。"
run "いま何時かな。"
run "ありがとう、またね。"

echo ""
echo "======== サーバー側ログ ========"
grep -E "STT:|tool |spoke |restored |LLM:" "$LOG"
echo ""
echo "======== プロンプトキャッシュ再利用 ========"
sudo journalctl -u ollama --since "-15min" --no-pager | grep "loading cache slot" | sed "s/.*cache=/cache=/" | tail -12
