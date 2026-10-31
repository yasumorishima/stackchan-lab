#!/usr/bin/env bash
# クラウド環境で VOICEVOX・Ollama・サーバー（8000 番）を立ち上げる。
# cloud_setup.sh を済ませた後に使う。止めるときは cloud_up.sh stop。
#
#   ./cloud_up.sh          さくらの代役に手元の qwen2.5:3b を使う
#   SAKURA_TOKEN が環境変数にあれば本物のさくらへ投げる（無償枠を消費する）
set -u
cd "$(dirname "$0")"

if [ "${1:-}" = stop ]; then
  [ -f .cloud_server.pid ] && kill "$(cat .cloud_server.pid)" 2>/dev/null
  rm -f .cloud_server.pid
  docker rm -f voicevox >/dev/null 2>&1
  exit 0
fi

docker info >/dev/null 2>&1 || {
  nohup dockerd >/tmp/dockerd.log 2>&1 &
  for _ in $(seq 1 30); do docker info >/dev/null 2>&1 && break; sleep 1; done; }
docker start voicevox >/dev/null 2>&1 || docker run -d --name voicevox \
  -p 127.0.0.1:50021:50021 voicevox/voicevox_engine:cpu-0.25.2 >/dev/null
pgrep -x ollama >/dev/null || { nohup ollama serve >/tmp/ollama.log 2>&1 & sleep 3; }

export PORT=8000 PUBLIC_HOST=127.0.0.1 WS_TOKEN=stackchan-local
export STT_BACKEND=sherpa TTS_BACKEND="${TTS_BACKEND:-openjtalk}" LLM_BACKEND=sakura
export FALLBACK_LLM_BASE=http://127.0.0.1:11434 FALLBACK_LLM_MODEL=qwen2.5:3b
# 歌の音源と譜面は非公開レポ stackchan-cache に置いてある（clone してあれば使う）
CACHE_REPO="${CACHE_REPO:-/home/user/stackchan-cache}"
if [ -d "$CACHE_REPO/songs" ]; then
  export CHEER_CACHE="$CACHE_REPO/songs" CHEER_SHEET_DIR="$CACHE_REPO/sheets"
fi
if [ -z "${SAKURA_TOKEN:-}" ]; then
  # 代役を主にする（FALLBACK と同じ宛先にすると app.py は二重に試さない）
  export SAKURA_BASE=http://127.0.0.1:11434 SAKURA_MODEL=qwen2.5:3b SAKURA_TOKEN=dummy
fi

[ -f .cloud_server.pid ] && kill "$(cat .cloud_server.pid)" 2>/dev/null
nohup ./.venv/bin/python app.py > server.log 2>&1 &
echo $! > .cloud_server.pid

for _ in $(seq 1 60); do
  curl -s -m 2 http://127.0.0.1:50021/version >/dev/null && \
    grep -q "Running on" server.log && break
  sleep 2
done
grep -m1 backends server.log
# 代役の初回は 20 秒ほどかかるので先に温めておく（試験の待ち時間切れを防ぐ）
SAKURA_BASE=http://127.0.0.1:11434 SAKURA_MODEL=qwen2.5:3b \
  ./.venv/bin/python warm_llm.py 2>&1 | tail -1
echo "cloud_up: http://127.0.0.1:8000/xiaozhi/ota/"
