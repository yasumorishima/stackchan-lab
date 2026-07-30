#!/usr/bin/env bash
# ネットが切れている状態（本番 LLM に届かない）で、ローカルの小さいモデルへ
# 落ちて会話が成立するかを音声経路まで含めて確かめる。
#
# RPi5 は共有機なので推論回数は最小限（warm 1 + 発話 2）に留める。
# 本番の systemd（8000番・llm=dry）は止めない。
set -u
cd /home/yasu/stackchan-server
set -a
. ./server.conf
set +a
export PORT=8002
export PUBLIC_HOST=127.0.0.1
export LLM_BACKEND=sakura
# 本番の宛先は「誰も listen していない」＝ネット断の再現
export SAKURA_BASE=http://127.0.0.1:9
export SAKURA_MODEL=gpt-oss-120b
export SAKURA_TOKEN=dummy
export FALLBACK_LLM_BASE=http://127.0.0.1:11434
export FALLBACK_LLM_MODEL=qwen2.5:3b
export STT_BACKEND=sherpa
export TTS_BACKEND=voicevox

LOG=/home/yasu/stackchan-server/e2e_offline.log
: > "$LOG"
fuser -k 8002/tcp 2>/dev/null || true
sleep 1

./.venv/bin/python app.py >> "$LOG" 2>&1 &
SRV=$!
trap 'kill $SRV 2>/dev/null' EXIT
sleep 14
echo "== server 8002 起動 =="
grep -m1 backends "$LOG" || true

SAKURA_BASE=http://127.0.0.1:11434 SAKURA_MODEL=qwen2.5:3b \
  ./.venv/bin/python warm_llm.py
echo "== モデル warm 済み =="

run() {
  echo ""
  echo "######## $1"
  OTA_URL=http://127.0.0.1:8002/xiaozhi/ota/ TEST_TEXT="$1" \
    timeout 240 ./.venv/bin/python test_client.py 2>&1 \
    | grep -E "\"type\": \"stt\"|sentence_start|RESULT|Traceback|Error"
}

run "あしたの大阪の天気を教えて。"
run "じゃあ鳥取はどう？"

echo ""
echo "======== サーバー側ログ ========"
grep -E "STT:|tool |spoke |restored |LLM:|切り替え|ローカル LLM|when を" "$LOG"
