#!/usr/bin/env bash
# Claude Code のクラウド環境（Ubuntu・x86_64・root）に、サーバーの試験に要るものを入れる。
# 環境設定の「Setup script」から呼ぶ。何度走らせても同じ結果になる。
#
#   入るもの: venv（aiohttp / numpy / sherpa-onnx / duckdb）・Open JTalk・libopus・
#             音声認識モデル（ReazonSpeech k2 v2 int8・160MB）・Docker の VOICEVOX・
#             Ollama と qwen2.5:3b（さくらの代役）
#   入らないもの: 秘密（SAKURA_TOKEN / ODPT_TOKEN）と cache/（歌の音源・譜面）
set -eu
cd "$(dirname "$0")"

apt-get update -q
apt-get install -y -q open-jtalk open-jtalk-mecab-naist-jdic \
  hts-voice-nitech-jp-atr503-m001 libopus0 zstd

[ -x .venv/bin/python ] || python3 -m venv .venv
./.venv/bin/pip install -q aiohttp numpy sherpa-onnx duckdb

M=~/models/reazonspeech-k2-v2
mkdir -p "$M"
for f in encoder-epoch-99-avg-1.int8.onnx decoder-epoch-99-avg-1.int8.onnx \
         joiner-epoch-99-avg-1.int8.onnx tokens.txt; do
  [ -s "$M/$f" ] || curl -fsSL -o "$M/$f" \
    "https://huggingface.co/csukuangfj/reazonspeech-k2-v2/resolve/main/$f"
done

# VOICEVOX（歌と voicevox 読み上げ）。dockerd は常駐していないので自分で上げる
if ! docker info >/dev/null 2>&1; then
  nohup dockerd >/tmp/dockerd.log 2>&1 &
  for _ in $(seq 1 30); do docker info >/dev/null 2>&1 && break; sleep 1; done
fi
docker pull -q voicevox/voicevox_engine:cpu-0.25.2

# Ollama（さくらに届かない時の代役。本番の FALLBACK と同じモデル）
command -v ollama >/dev/null || curl -fsSL https://ollama.com/install.sh | sh
pgrep -x ollama >/dev/null || { nohup ollama serve >/tmp/ollama.log 2>&1 & sleep 3; }
ollama pull qwen2.5:3b

echo "cloud_setup: done"
