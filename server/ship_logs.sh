#!/usr/bin/env bash
# server.log の増えた分を、非公開のログ用レポへ日付ごとに追記して push する。
# systemd の stackchan-logs.timer から 10 分ごとに呼ぶ（手で叩いてもよい）。
#
#   LOG_REPO   ログ用レポの clone 先（既定 ~/stackchan-logs）
#   SERVER_LOG 元のログ（既定 このスクリプトの隣の server.log）
#
# 前回どこまで送ったかは LOG_REPO/.offset に持つ（ログが切り詰められたら頭から）。
# トークンらしき値は念のため伏せる（今のログには出ていない）。
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
LOG_REPO="${LOG_REPO:-$HOME/stackchan-logs}"
SERVER_LOG="${SERVER_LOG:-$HERE/server.log}"
OFF="$LOG_REPO/.offset"

cd "$LOG_REPO"
git pull -q --rebase || true

size=$(stat -c %s "$SERVER_LOG")
off=$(cat "$OFF" 2>/dev/null || echo 0)
[ "$size" -lt "$off" ] && off=0          # ログが作り直された
[ "$size" -eq "$off" ] && exit 0

mkdir -p logs
# 行の先頭の日付（2026-10-31 ...）で振り分ける。日付の無い行（Traceback 等）は直前の日（無ければ今日）へ
tail -c +"$((off + 1))" "$SERVER_LOG" | head -c "$((size - off))" \
  | sed -E 's/((token|TOKEN|Bearer|Authorization)["=: ]+)[A-Za-z0-9._-]{8,}/\1***/g' \
  | awk -v dir=logs -v day="$(date +%F)" '
      /^[0-9]{4}-[0-9]{2}-[0-9]{2} / { day = substr($0, 1, 10) }
      { print >> (dir "/" day ".log") }'
echo "$size" > "$OFF"

git add -A
git diff --cached --quiet && exit 0
git commit -q -m "logs $(date '+%Y-%m-%d %H:%M')"
git push -q
