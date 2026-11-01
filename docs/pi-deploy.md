# クラウドから本番（RPi5）へ出す

Claude Code のクラウド環境からは家の LAN にある Pi に入れない。代わりに **Pi が 2 分ごとに GitHub の `main` を見に来て**、変わっていれば自分で試験して入れ替える。

```
クラウド: ブランチで直す → PR → main に merge
   ↓（最大 2 分）
Pi: main の server/ と本番を比べる
   → 違うファイルだけを本番の写し（~/stackchan-deploy/stage）に重ねる
   → 写しのサーバーを 8003 番で立て、server/deploy_tests.txt の試験を回す（落ちたら 3 回まで）
   → 全部通ったら本番へ入れ替えて再起動 → /health と起動ログを確認
   → だめなら入れ替え前へ自動で戻す
   → 結果を非公開リポ stackchan-logs の deploy/ に push
クラウド: stackchan-logs の deploy/LATEST.md を読む
```

## 結果の見方

`yasumorishima/stackchan-logs` の `deploy/LATEST.md`（毎回上書き）と `deploy/<日付>/<時刻>-<sha>-<状態>.md`（履歴）。状態は次のどれか。

| 状態 | 意味 | 本番 |
|---|---|---|
| `DEPLOYED` | 試験に通り、入れ替えて確認も通った | 新しい |
| `NOOP` | server/ が本番と同じ（docs だけの変更など） | そのまま |
| `FAILED` | 8003 番の写しで試験に 3 回落ちた・写しが起動しない | 触っていない（依頼は走る） |
| `ROLLED_BACK` | 入れ替え後に /health が返らない・起動ログに Traceback | 元に戻した |
| `CRASH_RECOVERED` / `CRASHED_ROLLED_BACK` | 入れ替え中に deploy.sh が落ちた（Pi の再起動を含む） | 元に戻した |
| `RETRY` | Pi が混んでいる・8003 番が使用中（コードと無関係） | 10 分あけて最大 3 回 |
| `GAVE_UP` | RETRY が 3 回続いた | 触っていない |
| `DEFERRED` | 本体がつながっているので入れ替えを待っている（2 分おきに見て最大 15 回。報告は最初の 1 回だけ） | そのまま |

**`RETRY` と `DEFERRED` 以外で終わった sha は二度と試さない。** もう一度試すときは、直した commit を main に入れる（中身が同じでも新しい commit なら試す）。報告の push に失敗したら Pi に溜めておき、次の回にまとめて送る。

## 決まりごと

- **本番に残すもの**（リポの版で上書きしない）: `server.conf`（秘密）・`llm_usage.json`（さくらの利用回数）・`test_client.py`（本番は本体の実 MAC）。`cache/`（歌の音源・譜面）と `.venv` も触らない。
- **リポで消したファイルは本番に残る**（報告に「リポから消えたファイル」として出る）。消すなら下の依頼で。
- **試験は Pi で回る**。Pi は他の作業と共用なので、重い試験を `deploy_tests.txt` に足さない。試験はさくらの本番 LLM を使う（無料枠 3,000 回/月）。写しのサーバーはローカルの 3B へは落ちない設定。
- **新しい pip のモジュールを使う変更**は、先に依頼で `pip install` してから merge する（入っていないと写しが起動せず `FAILED`）。

## Pi で何かを走らせる（依頼）

コードを変えずに Pi で走らせたいとき（歌の作り直し・probe・pip install・ログの確認など）は、`ops/requests/<名前>.sh` を足して main に入れる。

- 入れ替えのあとに走る。試験に落ちた（`FAILED`）ときも走る＝足りない `pip install` は同じ commit で足せばよい

- 本番フォルダ（`~/stackchan-server`）で、本番の `server.conf` を読んだ環境で `bash` で 1 回だけ走る（中身を変えればもう一度走る）
- 上限 30 分。出力の末尾 80 行が報告に載る（server.conf の値は伏せ字になる）
- 再起動はしない。必要なら依頼の中で `sudo -n systemctl restart stackchan-server`

例: `ops/requests/2026-11-01-prerender.sh`

```bash
sudo -n docker start voicevox && sleep 25
nice -n 19 ./.venv/bin/python prerender.py
sudo -n docker stop voicevox
```

## Pi 側の仕組み

- `tools/pi-deploy/poll.sh` … 入口。Pi の `~/stackchan-deploy/poll.sh` に置いた**写し**が走る（自動更新しない。戻す処理と伏せ字の処理はここ）。変えたら Pi で `bash tools/pi-deploy/install.sh` を入れ直す（クラウドからはできない）。
- `tools/pi-deploy/deploy.sh` … 本体。**毎回 main の版が使われる**ので、クラウドから直せる。
- `stackchan-deploy.timer`（前回の終わりから 2 分）→ `stackchan-deploy.service`（yasu・nice 10・I/O idle）。
- 手で試すとき（Pi で）: `DRY_RUN=1`（入れ替え直前で止める・依頼も走らせない）と `NO_PUSH=1`（報告を push せず `~/stackchan-deploy/reports/unsent/` に置く）。
- 状態は `~/stackchan-deploy/state/`（`deployed_sha`・`done`＝試し済みの sha）、控えは `~/stackchan-deploy/backups/`（最新 10 個）。
