#!/usr/bin/env bash
# Pi に 1 回だけ入れる（poll.sh を変えたときも、これで入れ直す）。
#   bash tools/pi-deploy/install.sh
# poll.sh は自動更新しない＝クラウドからは入れ直せない。変えたら Pi で手で走らせる。
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
BASE="$HOME/stackchan-deploy"
mkdir -p "$BASE/state" "$BASE/reports" "$BASE/backups"
install -m 755 "$HERE/poll.sh" "$BASE/poll.sh"
sudo -n install -m 644 "$HERE/stackchan-deploy.service" /etc/systemd/system/stackchan-deploy.service
sudo -n install -m 644 "$HERE/stackchan-deploy.timer" /etc/systemd/system/stackchan-deploy.timer
sudo -n systemctl daemon-reload
sudo -n systemctl enable --now stackchan-deploy.timer
systemctl list-timers stackchan-deploy.timer --no-pager | head -3
