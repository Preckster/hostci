#!/usr/bin/env bash
# Install or update hostci on this host (run as root from a checkout).
set -euo pipefail
cd "$(dirname "$0")"
id ci >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/hostci --shell /usr/sbin/nologin ci
install -d -m 0755 -o ci -g ci /var/lib/hostci /var/lib/hostci/work /var/lib/hostci/cache
install -d -m 0700 /var/lib/hostci-state /etc/hostci
install -D -m 0755 hostci.py /opt/hostci/hostci.py
[ -f /etc/hostci/config.toml ] || install -m 0600 config.example.toml /etc/hostci/config.toml
install -m 0644 systemd/hostci.service systemd/hostci.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now hostci.timer
systemctl list-timers hostci.timer --no-pager
