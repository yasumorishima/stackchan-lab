#!/usr/bin/env bash
# Pi 側の固定の入口。stackchan-deploy.timer から 2 分ごとに呼ばれる。
# ~/stackchan-deploy/poll.sh に置いた写しが走る（リポの版で自動更新はしない）。
#
#   1. GitHub の main の sha を見る（ls-remote だけ＝変わっていなければここで終わる）
#   2. 変わっていれば取り込み、その sha の tools/pi-deploy/deploy.sh を子として走らせる
#   3. 結果を非公開の stackchan-logs の deploy/ へ push する（秘密は伏せてから）
#
# 入れ替えの途中で deploy.sh が落ちても、ここが本番を戻して報告する
# （戻す処理と秘密を伏せる処理はリポから書き換えられないよう、こちらに置く）。
set -u
BASE="$HOME/stackchan-deploy"
REPO="$BASE/repo"; STATE="$BASE/state"; OUT="$BASE/reports"; LOGS="$BASE/logs"
PROD="$HOME/stackchan-server"
URL="https://github.com/yasumorishima/stackchan-lab.git"
LOGS_URL="git@github.com:yasumorishima/stackchan-logs.git"
KEY="$HOME/.ssh/stackchan_logs"
MAX_RETRY=3
mkdir -p "$STATE" "$OUT" "$BASE/backups"
exec 9>"$BASE/lock"; flock -n 9 || exit 0

health() {  # health <port> <秒>
  local i
  for i in $(seq 1 "$2"); do
    curl -fsS -m 3 "http://127.0.0.1:$1/health" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

restore() {  # 入れ替え前の本番へ戻す
  local m="$STATE/swap_in_progress" tar new f
  tar=$(sed -n 1p "$m"); new=$(sed -n 2p "$m")
  [ -f "$tar" ] && tar -xpf "$tar" -C "$PROD"
  if [ -f "$new" ]; then
    while IFS= read -r f; do [ -n "$f" ] && rm -f -- "$PROD/$f"; done < "$new"
  fi
  sudo -n systemctl restart stackchan-server
  if health 8000 90; then echo "restore: /health OK"; else echo "restore: /health NG"; fi
  rm -f "$m"
}

publish() {  # publish <状態> <sha>   報告を伏せ字にして stackchan-logs へ
  local st="$1" sha="$2" day now name body
  day=$(date +%F); now=$(date +%H%M%S)
  name="deploy/$day/$now-${sha:0:7}-$st.md"
  body="$OUT/final.md"
  {
    echo "# $st ${sha:0:7}"
    echo
    echo "- 時刻: $(date '+%F %T %Z')"
    echo "- sha: $sha"
    echo
    [ -f "$OUT/report.md" ] && cat "$OUT/report.md"
    echo
    echo "## deploy.sh の出力（末尾 120 行）"
    echo '```'
    [ -f "$OUT/run.log" ] && tail -n 120 "$OUT/run.log"
    echo '```'
  } > "$body.raw"
  # 秘密を伏せる: server.conf の値を文字どおり＋トークンらしき形。残っていたら push しない
  if ! /usr/bin/python3 -I - "$PROD/server.conf" "$body.raw" "$body" <<'PY'
import re, sys
conf, src, dst = sys.argv[1:4]
vals = []
for line in open(conf, encoding="utf-8", errors="replace"):
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    v = line.split("=", 1)[1].strip().strip('"').strip("'")
    if len(v) >= 8 and not v.startswith(("/", "http://127.", "http://localhost")):
        vals.append(v)
t = open(src, encoding="utf-8", errors="replace").read()
for v in sorted(set(vals), key=len, reverse=True):
    t = t.replace(v, "***")
t = re.sub(r'((?:token|TOKEN|Bearer|Authorization|api_key|apikey)["=: ]+)[A-Za-z0-9._-]{8,}', r"\1***", t)
t = re.sub(r"\b(sk|sb_secret|ghp|gho|github_pat)_[A-Za-z0-9_]{8,}", "***", t)
if any(v in t for v in vals):
    sys.exit(1)
open(dst, "w", encoding="utf-8").write(t)
PY
  then
    echo "publish: 伏せ字に失敗したので push しない（$body.raw はローカルに残す）"
    return 1
  fi
  rm -f "$body.raw"
  if [ "${NO_PUSH:-0}" = 1 ]; then echo "NO_PUSH: $body に書いただけ"; return 0; fi
  if [ ! -d "$LOGS/.git" ]; then
    GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes" git clone -q "$LOGS_URL" "$LOGS" || return 1
    git -C "$LOGS" config core.sshCommand "ssh -i $KEY -o IdentitiesOnly=yes"
  fi
  local i
  for i in 1 2 3; do
    git -C "$LOGS" pull -q --rebase || true
    mkdir -p "$LOGS/deploy/$day"
    cp "$body" "$LOGS/$name"; cp "$body" "$LOGS/deploy/LATEST.md"
    git -C "$LOGS" add -A deploy
    git -C "$LOGS" diff --cached --quiet && return 0
    git -C "$LOGS" commit -q -m "deploy $st ${sha:0:7}"
    git -C "$LOGS" push -q && return 0
    git -C "$LOGS" reset -q --soft HEAD~1
    sleep 5
  done
  return 1
}

# 前回の入れ替えが途中で死んでいたら、まず戻す
if [ -f "$STATE/swap_in_progress" ]; then
  : > "$OUT/report.md"
  { echo "前回の入れ替えが途中で止まっていたので、入れ替え前へ戻した。"; restore; } > "$OUT/run.log" 2>&1
  publish CRASH_RECOVERED "$(cat "$STATE/attempting" 2>/dev/null || echo unknown)"
fi

remote=$(timeout 30 git ls-remote "$URL" refs/heads/main 2>/dev/null | cut -f1)
[ -n "$remote" ] || exit 0                              # ネットが無い
[ "$remote" = "$(cat "$STATE/deployed_sha" 2>/dev/null)" ] && exit 0
grep -q "^$remote " "$STATE/done" 2>/dev/null && exit 0   # 失敗済みの sha はやり直さない
nt=$(cat "$STATE/next_try_$remote" 2>/dev/null || echo 0)
[ "$(date +%s)" -lt "$nt" ] && exit 0                   # 待ち時間中

if [ ! -d "$REPO/.git" ]; then git clone -q "$URL" "$REPO" || exit 0; fi
git -C "$REPO" fetch -q origin main || exit 0
sha=$(git -C "$REPO" rev-parse FETCH_HEAD)
git -C "$REPO" checkout -q --detach "$sha" || exit 0
git -C "$REPO" clean -qfdx
echo "$sha" > "$STATE/attempting"

: > "$OUT/report.md"; echo CRASHED > "$OUT/status"
DEFER_COUNT=$(cat "$STATE/defer_$sha" 2>/dev/null || echo 0) \
PREV_SHA=$(cat "$STATE/deployed_sha" 2>/dev/null || echo) \
  timeout 5400 bash "$REPO/tools/pi-deploy/deploy.sh" "$sha" > "$OUT/run.log" 2>&1
rc=$?
st=$(cat "$OUT/status")
echo "deploy.sh rc=$rc status=$st" >> "$OUT/run.log"

if [ -f "$STATE/swap_in_progress" ]; then               # 入れ替え中に落ちた／事後確認で NG
  restore >> "$OUT/run.log" 2>&1
  [ "$st" = ROLLED_BACK ] || st=CRASHED_ROLLED_BACK
fi

case "$st" in
  DEPLOYED|NOOP)
    echo "$sha" > "$STATE/deployed_sha"
    rm -f "$STATE"/retry_* "$STATE"/next_try_* "$STATE"/defer_* "$STATE"/notified_*
    publish "$st" "$sha" ;;
  RETRY)
    n=$(( $(cat "$STATE/retry_$sha" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$STATE/retry_$sha"
    if [ "$n" -ge "$MAX_RETRY" ]; then
      echo "$sha GAVE_UP" >> "$STATE/done"; publish GAVE_UP "$sha"
    else
      echo $(( $(date +%s) + 600 * n )) > "$STATE/next_try_$sha"
      [ -f "$STATE/notified_retry_$sha" ] || { publish RETRY "$sha"; touch "$STATE/notified_retry_$sha"; }
    fi ;;
  DEFERRED)
    echo $(( DEFER_COUNT_NOW = $(cat "$STATE/defer_$sha" 2>/dev/null || echo 0) + 1 )) > "$STATE/defer_$sha"
    [ -f "$STATE/notified_defer_$sha" ] || { publish DEFERRED "$sha"; touch "$STATE/notified_defer_$sha"; } ;;
  *)
    echo "$sha $st" >> "$STATE/done"
    publish "$st" "$sha" ;;
esac
rm -f "$STATE/attempting"
