# 依頼の経路の確認（読むだけ）: 本番の状態を表示する
systemctl is-active stackchan-server
curl -fsS -m 3 http://127.0.0.1:8000/health; echo
uptime
df -h ~ | tail -1
ls cache/songs | wc -l
