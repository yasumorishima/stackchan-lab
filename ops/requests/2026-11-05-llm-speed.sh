# さくらの AI Engine のモデルを返事の速さで比べる（読むだけ・本番の設定は変えない）。
# いまの gpt-oss-120b（考える量 low / 既定）と、より軽いモデルを、2 問 × 3 回ずつ測る。
# 1 回 1 リクエスト。全部で 50 リクエスト前後（gpt-oss-120b は無償枠から、プレビューは従量で数円以下）。
set -u
set -a; . ./server.conf; set +a
nice -n 10 ./.venv/bin/python bench_llm_speed.py
