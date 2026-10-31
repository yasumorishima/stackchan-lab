#!/usr/bin/env bash
# クラウドの Claude Code から本番へ反映するための「取りに行く」反映。
# Pi へは外から入れないので、Pi の側から stackchan-lab の deploy ブランチを見に行く。
# stackchan-deploy.timer から 10 分ごとに呼ぶ（常駐しない一回実行）。
#
#   1. deploy ブランチが前回反映した commit から進んでいれば
#   2. server/ 直下で変わったファイルだけを退避して上書き
#   3. 構文確認と試験（test_clean.py / test_sheet_bars.py）。落ちたら元に戻す
#   4. 歌の割り振りに関わるファイルが変わっていたら VOICEVOX を一時的に立てて prerender
#   5. 再起動して /health と起動ログを確かめる。駄目なら元に戻して再起動
#   6. 経過は server.log に「deploy:」で書く（ログ用レポ経由でクラウドが読める）
#
#   初回だけ: ./pull_deploy.sh --init <今の本番に当たる commit>
#
# 触らないもの: server.conf・llm_usage.json・cache/・.venv/・*.service/*.timer
# （systemd の定義は sudo cp が要るので、変わっていたら報告だけする）
set -u

main() {
  HERE="$(cd "$(dirname "$0")" && pwd)"
  REPO="${DEPLOY_REPO:-$HOME/stackchan-lab}"
  BRANCH="${DEPLOY_BRANCH:-deploy}"
  STATE="$HERE/.deployed_commit"
  LOG="$HERE/server.log"
  SERVICE="${DEPLOY_SERVICE:-stackchan-server}"
  VV_IMAGE="${VOICEVOX_IMAGE:-voicevox/voicevox_engine:cpu-arm64-0.25.2}"
  PORT="${PORT:-8000}"

  # 起動したものに錠の fd を渡さない（下の 9>&-）。渡すと次の回が錠を取れない
  exec 9>"$HERE/.deploy.lock"
  flock -n 9 || exit 0

  cd "$REPO" || { say "リポジトリが無い: $REPO"; exit 1; }
  if [ "${1:-}" = "--init" ]; then
    git rev-parse --verify -q "${2:-}^{commit}" >/dev/null \
      || { echo "commit が分からない: ${2:-}"; exit 1; }
    git rev-parse "$2" > "$STATE"
    echo "基準を $(cut -c1-7 "$STATE") にしました"
    exit 0
  fi
  [ -s "$STATE" ] || exit 0          # 初期化されていなければ何もしない

  git fetch -q origin "$BRANCH" 2>/dev/null || exit 0
  new=$(git rev-parse "origin/$BRANCH")
  old=$(cat "$STATE")
  [ "$new" = "$old" ] && exit 0
  # 一度落ちた commit は、次の commit が来るまで試し直さない（毎回元に戻すだけになる）
  [ "$new" = "$(cat "$STATE.failed" 2>/dev/null)" ] && exit 0
  [ "$new" = "$(cat "$STATE.refused" 2>/dev/null)" ] && exit 0
  restarted=0
  start=$(stat -c %s "$LOG" 2>/dev/null || echo 0)
  if ! git merge-base --is-ancestor "$old" "$new" 2>/dev/null; then
    # 前回の反映より前へ戻されたり別の歴史になったりした。勝手には進めない
    say "deploy ブランチが前回 ${old:0:7} の子孫ではないので止めた（${new:0:7}）"
    git rev-parse "$new" > "$STATE.refused"
    exit 1
  fi

  say "開始 ${old:0:7} → ${new:0:7}: $(git log -1 --format=%s "$new")"
  files=()
  units=()
  while IFS= read -r f; do
    name="${f#server/}"
    case "$name" in
      */*|server.conf*|llm_usage.json) continue ;;
      *.service|*.timer) units+=("$name"); continue ;;
      *.py|*.sh|*.json|*.md|*.example) files+=("$name") ;;
    esac
  done < <(git diff --name-only "$old" "$new" -- server/)
  [ ${#units[@]} -gt 0 ] && say "systemd の定義が変わっている（手で反映が要る）: ${units[*]}"
  if [ ${#files[@]} -eq 0 ]; then
    say "反映するファイルが無い"
    echo "$new" > "$STATE"
    exit 0
  fi

  bak="$HOME/claude-scratch/deploy-$(date +%Y%m%d-%H%M%S)-${new:0:7}"
  mkdir -p "$bak"
  added=()
  for f in "${files[@]}"; do
    if [ -e "$HERE/$f" ]; then cp -p "$HERE/$f" "$bak/$f"; else added+=("$f"); fi
  done
  for f in "${files[@]}"; do
    if git cat-file -e "$new:server/$f" 2>/dev/null; then
      git show "$new:server/$f" > "$HERE/$f.deploy-tmp" && mv "$HERE/$f.deploy-tmp" "$HERE/$f"
      case "$f" in *.sh) chmod +x "$HERE/$f" ;; esac
    else
      rm -f "$HERE/$f"                  # ブランチで消されたファイル（退避はある）
    fi
  done
  say "上書き ${#files[@]} 件（退避 $bak）: ${files[*]}"

  cd "$HERE"
  py=()
  for f in "${files[@]}"; do case "$f" in *.py) [ -e "$f" ] && py+=("$f") ;; esac; done
  if [ ${#py[@]} -gt 0 ] && ! out=$(./.venv/bin/python -m py_compile "${py[@]}" 2>&1); then
    fail "構文エラー: $(echo "$out" | tail -1)"
    return
  fi
  for t in test_clean.py test_sheet_bars.py; do
    [ -e "$t" ] || continue
    if ! out=$(timeout 300 ./.venv/bin/python "$t" 2>&1); then
      fail "$t が落ちた: $(echo "$out" | grep -E '^NG' | head -3 | tr '\n' ' ')$(echo "$out" | tail -1)"
      return
    fi
    say "$t: $(echo "$out" | tail -1)"
  done

  if printf '%s\n' "${files[@]}" | grep -qE '^(sheet_song|cheer_song|sing_vv|transcribe|segment_dp)\.py$'; then
    prerender
  fi

  start=$(stat -c %s "$LOG" 2>/dev/null || echo 0)
  restarted=1
  if ! restart_ok; then
    fail "再起動後に動かない: $(tail -c +"$((start + 1))" "$LOG" | grep -E 'Traceback|Error' | tail -2 | tr '\n' ' ')"
    return
  fi
  echo "$new" > "$STATE"
  say "完了 ${new:0:7}（再起動 ${took}s・/health 応答あり）"
}

say() { echo "$(date '+%Y-%m-%d %H:%M:%S'),000 INFO deploy: $*" >> "$LOG"; echo "deploy: $*"; }

restart_ok() {
  local t0=$(date +%s.%N)
  sudo -n systemctl restart "$SERVICE" 9>&- || { say "sudo で再起動できない（sudoers の設定が要る）"; return 1; }
  took=$(printf '%.1f' "$(echo "$(date +%s.%N) - $t0" | bc)")
  for _ in $(seq 1 30); do
    curl -fsS -m 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break
    sleep 2
  done
  curl -fsS -m 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 || return 1
  ! tail -c +"$((start + 1))" "$LOG" | grep -q "MLB の道具は使えません"
}

fail() {
  say "失敗のため元に戻す: $*"
  for f in "${files[@]}"; do
    if [ -e "$bak/$f" ]; then cp -p "$bak/$f" "$HERE/$f"; else rm -f "$HERE/$f"; fi
  done
  if [ "$restarted" = 1 ]; then
    restart_ok && say "元に戻して再起動した（${new:0:7} は反映していない）" \
      || say "元に戻したが再起動を確かめられない。手で見てください"
  else
    say "元に戻した（再起動はしていない・${new:0:7} は反映していない）"
  fi
  echo "$new" > "$STATE.failed"
  exit 1
}

prerender() {
  local name=voicevox-deploy
  if ! docker run -d --rm --name "$name" -p 127.0.0.1:50021:50021 "$VV_IMAGE" >/dev/null 2>&1 9>&-; then
    say "VOICEVOX を立てられないので歌の作り直しは飛ばした（初回の歌が遅くなる）"
    return
  fi
  for _ in $(seq 1 60); do curl -fsS -m 2 http://127.0.0.1:50021/version >/dev/null 2>&1 && break; sleep 2; done
  out=$(timeout 1800 ./.venv/bin/python prerender.py 2>&1)
  say "prerender: $(echo "$out" | tail -2 | tr '\n' ' ')"
  docker rm -f "$name" >/dev/null 2>&1
}

main "$@"
exit
