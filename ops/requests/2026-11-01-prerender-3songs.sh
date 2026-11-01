# 歌詞の割り振りを直した 3 曲（神里和毅・捕手のテーマ・梶原昂希、8abb146）の歌を作り直す。
# 作り直さないと、初めて頼まれたときに VOICEVOX が止まっていて歌えない。
# VOICEVOX は使うときだけ立てて、終わったら止める（共有機なので nice で回す）。
set -u
if sudo -n docker start voicevox >/dev/null 2>&1; then
  made=0
else
  sudo -n docker run -d --rm --name voicevox -p 127.0.0.1:50021:50021 \
    voicevox/voicevox_engine:cpu-arm64-0.25.2 >/dev/null && made=1 || { echo "VOICEVOX を立てられない"; exit 1; }
fi
for _ in $(seq 1 60); do curl -fsS -m 2 http://127.0.0.1:50021/version >/dev/null 2>&1 && break; sleep 2; done
before=$(ls cache/songs/*.sung.*.wav 2>/dev/null | sort)
nice -n 19 ./.venv/bin/python prerender.py 2>&1 | tail -20
rc=${PIPESTATUS[0]}
sudo -n docker stop voicevox >/dev/null 2>&1
echo "--- 新しくできた歌"
comm -13 <(echo "$before") <(ls cache/songs/*.sung.*.wav 2>/dev/null | sort) | xargs -r -n1 basename
exit "$rc"
