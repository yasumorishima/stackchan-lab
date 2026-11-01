#!/usr/bin/env bash
# main の 1 つの sha を本番（~/stackchan-server）へ出す。poll.sh から子として呼ばれる。
# このファイルはリポの版が毎回使われる（クラウドから直せる）。
#
#   状態は $OUT/status に 1 語で書く:
#     NOOP        server/ に本番と違うファイルが無く、新しい依頼も無い
#     DEPLOYED    試験に通り、入れ替えて再起動し、事後確認も通った
#     FAILED      試験用サーバー（8003 番）での試験に落ちた＝本番は触っていない
#     ROLLED_BACK 入れ替え後の確認に落ちた＝poll.sh が入れ替え前へ戻す
#     RETRY       Pi が混んでいる・8003 番が使用中など、コードと関係ない理由
#     DEFERRED    本体とつながっている（8000 番に接続がある）ので入れ替えを待つ
#     DRYRUN      DRY_RUN=1 で試験まで（手で試すとき用。依頼も走らせない）
#
#   本番に残すもの（リポの版で上書きしない）: server.conf llm_usage.json test_client.py
#   試験の一覧: server/deploy_tests.txt（無ければ下の既定）
set -u
SHA="$1"
BASE="$HOME/stackchan-deploy"
REPO="$BASE/repo"; STATE="$BASE/state"; OUT="$BASE/reports"; STAGE="$BASE/stage"
PROD="$HOME/stackchan-server"
PY="$PROD/.venv/bin/python"
KEEP=" server.conf llm_usage.json test_client.py "
DEFAULT_TESTS="test_particle.py test_sheet_bars.py test_barge.py test_sing_e2e.py"
MAX_DEFER=15            # 2 分おき × 15 ＝ 30 分待っても会話中なら出す
SRV=""
TAB=$'\t'

status() { echo "$1" > "$OUT/status"; }
rep() { printf '%s\n' "$*" >> "$OUT/report.md"; }
fence() { echo '```' >> "$OUT/report.md"; }
finish() { [ -n "$SRV" ] && kill "$SRV" 2>/dev/null; wait 2>/dev/null; }
trap finish EXIT
health() {  # health <port> <秒> [見張る pid]
  local i
  for i in $(seq 1 "$2"); do
    curl -fsS -m 3 "http://127.0.0.1:$1/health" >/dev/null 2>&1 && return 0
    [ -n "${3:-}" ] && ! kill -0 "$3" 2>/dev/null && return 1
    sleep 1
  done
  return 1
}

run_requests() {  # 依頼の実行（本番フォルダで・本番の環境で）。DRY_RUN では走らせない
  [ ${#reqs[@]} -gt 0 ] || return 0
  if [ "${DRY_RUN:-0}" = 1 ]; then rep "DRY_RUN なので依頼 ${#reqs[@]} 本は走らせない。"; return 0; fi
  local r blob path rc
  (
    unset PORT PUBLIC_HOST HOST CHEER_CACHE CHEER_SHEET_DIR LLM_USAGE_PATH LOG FALLBACK_LLM_BASE
    set -a; . "$PROD/server.conf"; set +a
    for r in "${reqs[@]}"; do
      blob=${r%%$TAB*}; path=${r#*$TAB}
      rep "## 依頼 $path"
      ( cd "$PROD" && timeout 1800 bash "$REPO/$path" ) > "$OUT/req.out" 2>&1
      rc=$?
      rep "終了コード $rc"; fence; tail -n 80 "$OUT/req.out" >> "$OUT/report.md"; fence
      echo "$blob $path" >> "$STATE/requests_done"
    done
  )
}

echo "== sha $SHA  prev ${PREV_SHA:-（記録なし）}"

# --- Pi が混んでいれば今は出さない（共有機） ---
load=$(cut -d' ' -f1 /proc/loadavg)
mem=$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo)
if awk -v l="$load" 'BEGIN{exit !(l > 6)}' || [ "$mem" -lt 1500 ]; then
  rep "Pi が混んでいるので後でやり直す（load $load・空きメモリ ${mem}MB）。"
  status RETRY; exit 0
fi

# --- 本番と違うファイル ---
changed=()      # "mode<TAB>path"（path は server/ からの相対）
while IFS="$TAB" read -r meta path; do
  mode=${meta%% *}; rel=${path#server/}
  case "$KEEP" in *" $rel "*) continue ;; esac
  case "$rel" in cache/*|.venv/*|__pycache__/*|*/__pycache__/*) continue ;; esac
  if [ -e "$PROD/$rel" ] && git -C "$REPO" show "$SHA:$path" | cmp -s - "$PROD/$rel"; then
    want=0; [ "$mode" = 100755 ] && want=1
    have=0; [ -x "$PROD/$rel" ] && have=1
    [ "$want" = "$have" ] && continue
  fi
  changed+=("$mode$TAB$rel")
done < <(git -C "$REPO" -c core.quotePath=false ls-files -s server/)

# --- 新しい依頼（ops/requests/*.sh。中身ごとに 1 回だけ走る） ---
reqs=()
while IFS="$TAB" read -r meta path; do
  blob=$(echo "$meta" | cut -d' ' -f2)
  grep -qxF "$blob $path" "$STATE/requests_done" 2>/dev/null || reqs+=("$blob$TAB$path")
done < <(git -C "$REPO" -c core.quotePath=false ls-files -s 'ops/requests/*.sh')

if [ -n "${PREV_SHA:-}" ] && git -C "$REPO" cat-file -e "$PREV_SHA" 2>/dev/null; then
  gone=$(git -C "$REPO" diff --name-only --diff-filter=D "$PREV_SHA" "$SHA" -- server/ | sed 's#^server/##')
  if [ -n "$gone" ]; then rep "## リポから消えたファイル（本番には残してある）"; rep "$gone"; rep ""; fi
fi

if [ ${#changed[@]} -eq 0 ] && [ ${#reqs[@]} -eq 0 ]; then
  rep "server/ は本番と同じで、新しい依頼も無い。"
  status NOOP; exit 0
fi

if [ ${#changed[@]} -gt 0 ]; then
  rep "## 本番と違うファイル（${#changed[@]} 本）"
  for c in "${changed[@]}"; do
    rel=${c#*$TAB}
    if [ -e "$PROD/$rel" ]; then rep "- $rel"; else rep "- $rel（新規）"; fi
  done
  rep ""
fi

# 試験と依頼で共通の環境（本番の server.conf）
set -a; . "$PROD/server.conf"; set +a

if [ ${#changed[@]} -gt 0 ]; then
  # --- 会話中なら待つ（2 分おきに見て最大 MAX_DEFER 回） ---
  if [ "${DRY_RUN:-0}" != 1 ] && ss -Htn state established '( sport = :8000 )' | grep -q . \
     && [ "${DEFER_COUNT:-0}" -lt $MAX_DEFER ]; then
    rep "本体とつながっている（会話中かもしれない）ので入れ替えを待つ（${DEFER_COUNT:-0}/$MAX_DEFER）。"
    status DEFERRED; exit 0
  fi

  # --- 試験用の写し（本番の写し＋変更分）。キャッシュも写しを使う ---
  mkdir -p "$STAGE"
  rsync -a --delete --exclude '*.log' --exclude .venv --exclude __pycache__ "$PROD/" "$STAGE/"
  for c in "${changed[@]}"; do
    mode=${c%%$TAB*}; rel=${c#*$TAB}
    mkdir -p "$STAGE/$(dirname "$rel")"
    git -C "$REPO" show "$SHA:server/$rel" > "$STAGE/$rel"
    if [ "$mode" = 100755 ]; then chmod 755 "$STAGE/$rel"; else chmod 644 "$STAGE/$rel"; fi
  done

  if ss -Hltn '( sport = :8003 )' | grep -q .; then
    rep "8003 番が使用中なので後でやり直す。"; status RETRY; exit 0
  fi
  export PORT=8003 PUBLIC_HOST=127.0.0.1 HOST=127.0.0.1
  export CHEER_CACHE="$STAGE/cache/songs" CHEER_SHEET_DIR="$STAGE/cache/sheets"
  export LLM_USAGE_PATH="$STAGE/llm_usage.json" LOG="$STAGE/server.log"
  export FALLBACK_LLM_BASE=http://127.0.0.1:9      # 試験でローカルの 3B を起こさない
  : > "$STAGE/server.log"
  ( cd "$STAGE" && exec "$PY" app.py >> "$STAGE/server.log" 2>&1 ) &
  SRV=$!
  if ! health 8003 120 "$SRV"; then
    rep "## 試験用サーバーが起動しない（本番は触っていない）"
    fence; tail -n 40 "$STAGE/server.log" >> "$OUT/report.md"; fence
    if grep -q "ModuleNotFoundError" "$STAGE/server.log"; then
      rep "→ 足りないモジュールがある。Pi の venv に入れる必要がある（ops/requests で pip install を依頼できる）。"
    fi
    kill "$SRV" 2>/dev/null; wait "$SRV" 2>/dev/null; SRV=""
    status FAILED; run_requests; exit 0
  fi
  echo "== 試験用サーバー 8003 起動"

  tests=$(git -C "$REPO" show "$SHA:server/deploy_tests.txt" 2>/dev/null | sed 's/#.*//' | xargs)
  [ -n "$tests" ] || tests="$DEFAULT_TESTS"
  rep "## 試験（8003 番の写しで）"
  fail=0
  for t in $tests; do
    ok=0
    for try in 1 2 3; do   # モデルの返事の揺れで落ちることがある（2026-11-01 に test_barge で実測）
      if ( cd "$STAGE" && timeout 600 "$PY" "$t" ) > "$OUT/test.out" 2>&1; then ok=1; break; fi
      echo "== $t 失敗（$try 回目）"; tail -n 20 "$OUT/test.out"
    done
    if [ $ok = 1 ]; then
      if [ "$try" != 1 ]; then rep "- PASS $t（$try 回目で）"; else rep "- PASS $t"; fi
    else
      fail=1; rep "- FAIL $t"; fence; tail -n 30 "$OUT/test.out" >> "$OUT/report.md"; fence
    fi
  done
  kill "$SRV" 2>/dev/null; wait "$SRV" 2>/dev/null; SRV=""
  if [ $fail = 1 ]; then
    rep ""; rep "## 試験用サーバーのログ（末尾）"
    fence; tail -n 40 "$STAGE/server.log" >> "$OUT/report.md"; fence
    status FAILED; run_requests; exit 0
  fi
  rep ""

  if [ "${DRY_RUN:-0}" = 1 ]; then rep "DRY_RUN なので入れ替えない。"; status DRYRUN; exit 0; fi

  # --- 入れ替え ---
  ts=$(date +%Y%m%d-%H%M%S)
  tarf="$BASE/backups/$ts-${SHA:0:7}.tar"; newf="$BASE/backups/$ts-${SHA:0:7}.new"; lst="$BASE/backups/$ts.list"
  : > "$newf"; : > "$lst"
  for c in "${changed[@]}"; do
    rel=${c#*$TAB}
    if [ -e "$PROD/$rel" ]; then echo "$rel" >> "$lst"; else echo "$rel" >> "$newf"; fi
  done
  if ! tar -cpf "$tarf" -C "$PROD" -T "$lst"; then
    rep "控えの tar が作れない。入れ替えない。"; status FAILED; exit 0
  fi
  rm -f "$lst"
  printf '%s\n%s\n' "$tarf" "$newf" > "$STATE/swap_in_progress"
  for c in "${changed[@]}"; do
    rel=${c#*$TAB}
    mkdir -p "$PROD/$(dirname "$rel")"
    cp -p "$STAGE/$rel" "$PROD/$rel.deploytmp" && mv -f "$PROD/$rel.deploytmp" "$PROD/$rel"
  done
  sudo -n systemctl stop stackchan-server
  pre=$(stat -c %s "$PROD/server.log" 2>/dev/null || echo 0)
  sudo -n systemctl start stackchan-server
  if ! health 8000 120 || ! systemctl is-active -q stackchan-server; then
    rep "## 入れ替え後、本番が /health に答えない → 入れ替え前へ戻す"
    fence; tail -c +"$((pre + 1))" "$PROD/server.log" | tail -n 40 >> "$OUT/report.md"; fence
    status ROLLED_BACK; exit 0
  fi
  sleep 15
  if tail -c +"$((pre + 1))" "$PROD/server.log" | grep -q "Traceback"; then
    rep "## 入れ替え後の本番ログに Traceback → 入れ替え前へ戻す"
    fence; tail -c +"$((pre + 1))" "$PROD/server.log" | tail -n 40 >> "$OUT/report.md"; fence
    status ROLLED_BACK; exit 0
  fi
  rm -f "$STATE/swap_in_progress"
  status DEPLOYED
  rep "## 入れ替え済み（本番 8000 番の /health OK・起動後 15 秒 Traceback なし）"
  rep "戻し用の控え: $tarf"
  ls -1t "$BASE"/backups/*.tar 2>/dev/null | tail -n +11 | while read -r o; do rm -f "$o" "${o%.tar}.new"; done
  rep ""
fi

run_requests
if [ ${#changed[@]} -gt 0 ]; then status DEPLOYED; else status NOOP; fi
exit 0
