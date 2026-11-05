# さくらの AI Engine のモデルを返事の速さで比べる（読むだけ・本番の設定は変えない）。
# gpt-oss-120b だけを、考える量 low と既定で 2 問 × 3 回ずつ測る（無償枠のみ）。
# 全部で 12 リクエスト（無償枠 月 3,000 回の内）。ほかのモデルには投げない。
set -u
set -a; . ./server.conf; set +a
nice -n 10 ./.venv/bin/python bench_llm_speed.py
