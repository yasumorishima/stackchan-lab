# 牧の譜面（sheets/牧秀悟.json）と 1 音ずつの歌い方を入れて歌を作り直す。
# 非公開の stackchan-cache から cache/sheets/ へ入れ、歌を作り置きする。
# 既にある選手の譜面は上書きしない（読み直しで変わった筒香を勝手に替えない）。
set -u
export GIT_TERMINAL_PROMPT=0
export GIT_SSH_COMMAND="ssh -o BatchMode=yes -o ConnectTimeout=15"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
got=""
for url in git@github.com:yasumorishima/stackchan-cache.git \
           https://github.com/yasumorishima/stackchan-cache.git; do
  if timeout 600 git clone -q --depth 1 --filter=blob:none --sparse "$url" "$tmp/c" 2>"$tmp/err" &&
     git -C "$tmp/c" sparse-checkout set sheets 2>>"$tmp/err"; then
    got="$url"; break
  fi
  echo "取れない: $url: $(tail -1 "$tmp/err")"
  rm -rf "$tmp/c"
done
if [ -z "$got" ]; then
  echo "stackchan-cache を読めないので止める（Pi にこのレポの鍵が無い）"
  exit 1
fi
echo "取れた: $got ($(git -C "$tmp/c" rev-parse --short HEAD))"
mkdir -p cache/sheets
n=0
for f in "$tmp/c/sheets/"*.json; do
  b=$(basename "$f")
  if [ "$b" = team_songs.json ] || [ "$b" = sung_style.json ] || [ ! -e "cache/sheets/$b" ]; then
    cp "$f" "cache/sheets/$b" && n=$((n + 1))
  fi
done
echo "--- $n 件入れた。cache/sheets は $(ls cache/sheets/*.json | wc -l) 件"

if sudo -n docker start voicevox >/dev/null 2>&1; then
  :
else
  sudo -n docker run -d --rm --name voicevox -p 127.0.0.1:50021:50021 \
    voicevox/voicevox_engine:cpu-arm64-0.25.2 >/dev/null || { echo "VOICEVOX を立てられない"; exit 1; }
fi
for _ in $(seq 1 60); do curl -fsS -m 2 http://127.0.0.1:50021/version >/dev/null 2>&1 && break; sleep 2; done
before=$(ls cache/songs/*.sung.*.wav 2>/dev/null | sort)
nice -n 19 ./.venv/bin/python prerender.py 2>&1 | tail -40
rc=${PIPESTATUS[0]}
sudo -n docker stop voicevox >/dev/null 2>&1
echo "--- 新しくできた歌"
comm -13 <(echo "$before") <(ls cache/songs/*.sung.*.wav 2>/dev/null | sort) | xargs -r -n1 basename | sed "s/\.sung\..*//" | tr "\n" " "; echo
exit "$rc"
