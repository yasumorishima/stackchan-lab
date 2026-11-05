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
- Pi は自然に電源が切れる。ログや反映の報告が止まっていても、まず電源が切れているだけと考える（入れば溜まった main から反映される）
- 本番で上書きしないもの: `server.conf`・`llm_usage.json`・`test_client.py`・`cache/`・`.venv`
- `tools/pi-deploy/poll.sh` を変えたときだけ、ローカルで `install.sh` の入れ直しが要る
- Pi に Claude Code は入れない（共有機。常駐を増やさない）。Pi に入れるのは上の経路だけ

## 読めるもの
- `yasumorishima/stackchan-logs`: 本番の `server.log` が 10 分ごとに `logs/<日付>.log` へ届く。実機で何が起きたかはまずここ
- `yasumorishima/stackchan-cache`: 本番の `cache/`（`songs/` 音源と歌・`sheets/` 譜面・`marts/` MLB の表）。
  音源と歌詞は公開レポ（このレポ）に入れない

## LLM（決定済み・調べ直さない）
- 🔴 **無償枠で動かすのが絶対**（user 2026-11-05）。有料になるモデル・API には試験でも投げない
- **さくらの gpt-oss-120b のまま**（user 決定 2026-11-05）。さくらで無償枠（月 3,000 回）があるのはこれだけ。
  gpt-oss-20b はさくらに無い。ほかの軽いモデル（Qwen3.6-35B-A3B・gemma-4・llm-jp 等）は有料かプレビューなので使わない。
  Haiku など他社への乗り換えも提案しない
- 実測（Pi・2026-11-05・道具一覧付き）: gpt-oss-120b は最初の 1 文字まで 0.3 秒（low）/ 0.4 秒（既定）で、さくらの中で最速級。
  遅さはモデルではなく、1 文そろうまでの待ち・道具の往復・話し終わり判定の側にある
- 速さはモデルを替えずに詰める: `reasoning_effort=low`（`LLM_REASONING_EFFORT`）、歌の依頼は LLM を通さない、
  起動時の認識の温め。次の候補は話し終わり判定（無音 0.6 秒）の短縮と、よくある質問を LLM なしで答えること

## クラウドで試す
- `server/cloud_setup.sh` で依存・認識モデル・VOICEVOX・Ollama を入れ、`server/cloud_up.sh` で起動
  （`stackchan-cache` を `/home/user/stackchan-cache` に clone してあれば歌もそれを使う）
- `SAKURA_TOKEN` が無ければ LLM は qwen2.5:3b の代役。道具の選び方や SQL は本番の gpt-oss-120b より大きく劣るので、
  LLM の振る舞いの良し悪しは本番ログで判断する
- 試験を直接走らせると `server/llm_usage.json` が増える。**コミットせず戻す**
- 原曲の音源は演奏だけで歌声が無い。音声認識（Whisper 含む）では歌詞を取れない。歌詞の割り振りは譜面のリズムの繰り返しで確かめる

## 未解決
- 捕手のテーマの歌い出し「いざすすめ」は 8 音に 5 モーラ（3 音を伸ばしで埋めている）。実際に何と歌うか未確認

## 次にやること（2026-11-05 時点）
1. **返事の速さ**（アレクサ並みが目標）: 本番 11 月の中央値は 話し終わり→声 約 2.4 秒（＋無音待ち 0.6 秒）。
   LLM 単体は 0.3 秒なので、①1 文そろうまでの待ち（`SentenceFeed`）を短く ②道具の往復を減らす
   ③話し終わり判定の無音 0.6 秒を短く、を順に詰める。PR #12（low・歌は LLM なし）の効果を本番ログで先に測る
2. **チーム曲**: 歌詞は公式 https://www.baystars.co.jp/players/theme （読み付き）から取り込む。
   「得点の歌＝熱き星たちよ」「試合開始＝勝利の輝き」「負けてる時＝ライジングテーマ」「チャンステーマ0〜4」等の
   呼び方で選べるようにする。旋律はローカルの `~/pawapuro` で譜面を作ってもらう（依頼済み・松尾汐恩も）
3. user 様の返事待ち: 宮﨑・梶原の「少しずれ」の場所／佐野「け」の前の途切れ（VOICEVOX の k の閉鎖 0.1 秒）を
   全曲で短くしてよいか／筒香は作り直しの一覧で「見直しが要りそう」と出た
