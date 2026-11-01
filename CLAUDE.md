# stackchan-lab（Claude Code 向けの覚え書き）

スタックちゃん（M5STACK-K151）の自前サーバー（`server/`）と記録。本番は家の LAN の Raspberry Pi 5
（`~/stackchan-server`・systemd の `stackchan-server`・8000 番）。user 様への返事は日本語で、短く、結果から。

## 本番へ出す
- **main に merge すれば出る**。Pi が 2 分ごとに main を見て、`server/` の差分を写しに重ね、
  8003 番で `server/deploy_tests.txt` の試験 → 通れば入れ替え → `/health` と起動ログ確認 → だめなら自動で戻す
- 結果は非公開レポ `yasumorishima/stackchan-logs` の `deploy/LATEST.md`。詳しくは `docs/pi-deploy.md`
- Pi で 1 回だけ何か走らせる（prerender・pip install など）: `ops/requests/<日付>-<名前>.sh` を足して merge
- **歌の割り振り（`sheet_song.py` など）を変えたら、prerender の依頼も一緒に出す**。本番は普段 VOICEVOX を止めている
- 同じ sha は二度試さない。やり直しは新しい commit で
- 本番で上書きしないもの: `server.conf`・`llm_usage.json`・`test_client.py`・`cache/`・`.venv`
- `tools/pi-deploy/poll.sh` を変えたときだけ、ローカルで `install.sh` の入れ直しが要る
- Pi に Claude Code は入れない（共有機。常駐を増やさない）。Pi に入れるのは上の経路だけ

## 読めるもの
- `yasumorishima/stackchan-logs`: 本番の `server.log` が 10 分ごとに `logs/<日付>.log` へ届く。実機で何が起きたかはまずここ
- `yasumorishima/stackchan-cache`: 本番の `cache/`（`songs/` 音源と歌・`sheets/` 譜面・`marts/` MLB の表）。
  音源と歌詞は公開レポ（このレポ）に入れない

## クラウドで試す
- `server/cloud_setup.sh` で依存・認識モデル・VOICEVOX・Ollama を入れ、`server/cloud_up.sh` で起動
  （`stackchan-cache` を `/home/user/stackchan-cache` に clone してあれば歌もそれを使う）
- `SAKURA_TOKEN` が無ければ LLM は qwen2.5:3b の代役。道具の選び方や SQL は本番の gpt-oss-120b より大きく劣るので、
  LLM の振る舞いの良し悪しは本番ログで判断する
- 試験を直接走らせると `server/llm_usage.json` が増える。**コミットせず戻す**
- 原曲の音源は演奏だけで歌声が無い。音声認識（Whisper 含む）では歌詞を取れない。歌詞の割り振りは譜面のリズムの繰り返しで確かめる

## 未解決
- 捕手のテーマの歌い出し「いざすすめ」は 8 音に 5 モーラ（3 音を伸ばしで埋めている）。実際に何と歌うか未確認
