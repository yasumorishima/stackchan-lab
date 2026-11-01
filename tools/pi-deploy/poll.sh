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

publish() {  # publish <状態> <sha>   報告を伏せ字にして unsent/ に置き、push する
  local st="$1" sha="$2" day now name raw
  day=$(date +%F); now=$(date +%H%M%S)
  name="deploy/$day/$now-${sha:0:7}-$st.md"
  raw="$OUT/final.raw"
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
  } > "$raw"
  mkdir -p "$OUT/unsent/$(dirname "$name")"
  # 秘密を伏せる: server.conf の値を文字どおり＋鍵らしき形。伏せた後にまだ鍵らしき形が残れば本文は出さない
  if ! /usr/bin/python3 -I - "$PROD/server.conf" "$raw" "$OUT/unsent/$name" <<'PY'
import re, sys
conf, src, dst = sys.argv[1:4]
vals = []
for line in open(conf, encoding="utf-8", errors="replace"):
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    v = line.split("=", 1)[1].strip()
    v = re.sub(r"\s+#.*$", "", v).strip().strip('"').strip("'")
    if len(v) >= 6 and not v.startswith(("/", "http://127.", "http://localhost")):
        vals.append(v)
t = open(src, encoding="utf-8", errors="replace").read()
for v in sorted(set(vals), key=len, reverse=True):
    t = t.replace(v, "***")
pats = [
    r'((?:token|TOKEN|Token|secret|SECRET|password|PASSWORD|Bearer|Authorization|api_key|apikey|API_KEY)["\'=: ]+)[A-Za-z0-9._\-+/]{8,}',
    r"\b(?:sk|sb_secret|sb_publishable|ghp|gho|ghs|github_pat|hf|xox[abp])[-_][A-Za-z0-9_\-]{8,}",
    r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}",
]
for p in pats:
    t = re.sub(p, lambda m: (m.group(1) if m.lastindex else "") + "***", t)
t = re.sub(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", "***", t, flags=re.S)
left = [v for v in vals if v in t] + [p for p in pats if re.search(p, t)]
if "PRIVATE KEY-----" in t or left:
    sys.exit(1)
open(dst, "w", encoding="utf-8").write(t)
PY
  then
    cp "$raw" "$raw.$day-$now"
    printf '# %s %s\n\n- 時刻: %s\n- sha: %s\n\n伏せ字の検査に通らなかったので本文は出さない。Pi の %s に残した。\n' \
      "$st" "${sha:0:7}" "$(date '+%F %T %Z')" "$sha" "$raw.$day-$now" > "$OUT/unsent/$name"
  fi
  rm -f "$raw"
  flush
}

flush() {  # unsent/ にある報告を全部 stackchan-logs へ。届いたものだけ消す
  local f rel latest
  [ -d "$OUT/unsent/deploy" ] || return 0
  [ -n "$(find "$OUT/unsent/deploy" -type f -name '*.md' | head -1)" ] || return 0
  if [ "${NO_PUSH:-0}" = 1 ]; then echo "NO_PUSH: $OUT/unsent に置いただけ"; return 0; fi
  if [ ! -d "$LOGS/.git" ]; then
    GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes" git clone -q "$LOGS_URL" "$LOGS" || return 1
    git -C "$LOGS" config core.sshCommand "ssh -i $KEY -o IdentitiesOnly=yes"
  fi
  git -C "$LOGS" pull -q --rebase || return 1
  latest=""
  while IFS= read -r f; do
    rel=${f#"$OUT/unsent/"}
    mkdir -p "$LOGS/$(dirname "$rel")"; cp "$f" "$LOGS/$rel"; latest="$f"
  done < <(find "$OUT/unsent/deploy" -type f -name '*.md' | sort)
  [ -n "$latest" ] && cp "$latest" "$LOGS/deploy/LATEST.md"
  git -C "$LOGS" add -A deploy
  git -C "$LOGS" diff --cached --quiet || git -C "$LOGS" commit -q -m "deploy report $(date '+%F %T')"
  local i
  for i in 1 2 3; do
    if git -C "$LOGS" pull -q --rebase && git -C "$LOGS" push -q; then
      find "$OUT/unsent/deploy" -type f -name '*.md' -delete
      return 0
    fi
    sleep 5
  done
  return 1   # 届かなかった分は unsent/ とローカルの commit に残り、次の回で送る
}

flush > /dev/null 2>&1 || true   # 前の回で送れなかった報告

# 前回の入れ替えが途中で死んでいたら、まず戻す
if [ -f "$STATE/swap_in_progress" ]; then
  : > "$OUT/report.md"
  { echo "前回の入れ替えが途中で止まっていたので、入れ替え前へ戻した。"; restore; } > "$OUT/run.log" 2>&1
  a=$(cat "$STATE/attempting" 2>/dev/null || echo unknown)
  [ "$a" != unknown ] && echo "$a CRASH_RECOVERED" >> "$STATE/done"
  publish CRASH_RECOVERED "$a"; rm -f "$STATE/attempting"
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
  timeout 5400 bash "$REPO/tools/pi-deploy/deploy.sh" "$sha" > "$OUT/run.log" 2>&1 9>&-
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
  DRYRUN)
    publish DRYRUN "$sha" ;;
  *)
    echo "$sha $st" >> "$STATE/done"
    publish "$st" "$sha" ;;
esac
rm -f "$STATE/attempting"
