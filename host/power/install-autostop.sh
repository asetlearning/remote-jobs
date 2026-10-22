#!/bin/bash
# install-autostop.sh — make a paid cloud host stop itself when the runner is idle.
# Run as root on the host, after host/install.sh has installed the runner for the
# worker account (re-run that first: autostop needs a runner with `_power-check`).
#
#   sudo bash install-autostop.sh              install or update, then check the setup
#   sudo bash install-autostop.sh --uninstall  stop and remove the units (keeps /etc/rjob-power)
#
# First run: writes /etc/rjob-power/autostop.toml from the template and exits; fill
# it in (worker, instance id, CLI profile) and run again. See docs/power.md for the
# provider credential (Nebius: a service account whose group holds
# compute.instance-power-operator on this instance only).
#
# Installs: /usr/local/lib/rjob-power/{rjob-autostop,providers/}   root-owned code
#           /etc/systemd/system/rjob-autostop.{service,timer}     check every 5 min (root)
#           /etc/systemd/system/rjob-boot.service                 boot: clear flags, start queued jobs (worker)
#
# Created by: Alex Myasnikov
# Date: October 22, 2026
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ETC=/etc/rjob-power
CFG=$ETC/autostop.toml
LIB=/usr/local/lib/rjob-power
UNITS=/etc/systemd/system

[[ $(id -u) == 0 ]] || { echo "run as root: sudo bash $0" >&2; exit 1; }

if [[ "${1:-}" == --uninstall ]]; then
  systemctl disable --now rjob-autostop.timer rjob-boot.service 2>/dev/null || true
  rm -f "$UNITS"/rjob-autostop.{service,timer} "$UNITS"/rjob-boot.service
  rm -rf "$LIB"
  systemctl daemon-reload
  echo "autostop removed; $ETC kept (holds the config and credential)"
  exit 0
fi

install -d -o root -g root -m 700 "$ETC"
if [[ ! -f "$CFG" ]]; then
  install -o root -g root -m 600 "$HERE/autostop.toml.example" "$CFG"
  echo "wrote $CFG from the template: fill in worker, instance_id and profile, then run again" >&2
  exit 1
fi
chown root:root "$CFG"
chmod 600 "$CFG"

# Values for the boot unit, read with the system Python (root never runs worker-owned code).
read -r WORKER PY RUNNER ROOT < <(/usr/bin/python3 - "$CFG" <<'EOF'
import sys, tomllib
c = tomllib.load(open(sys.argv[1], "rb"))
print(c["worker"], c["runner_python"], c["runner"], c["rjob_root"])
EOF
)
id "$WORKER" >/dev/null || { echo "no such user: $WORKER" >&2; exit 1; }
if grep -q REPLACE-ME "$CFG"; then
  echo "$CFG still has placeholder values" >&2
  exit 1
fi

# The runner must know the autostop verbs (host/install.sh as the worker updates it).
WHOME=$(getent passwd "$WORKER" | cut -d: -f6)
if ! runuser -u "$WORKER" -- env HOME="$WHOME" RJOB_ROOT="$ROOT" "$PY" "$RUNNER" _power-check 30 0 >/dev/null; then
  echo "the runner at $RUNNER has no _power-check: re-run host/install.sh as $WORKER first" >&2
  exit 1
fi

install -d -o root -g root -m 755 "$LIB" "$LIB/providers"
install -o root -g root -m 755 "$HERE/rjob-autostop" "$LIB/rjob-autostop"
install -o root -g root -m 644 "$HERE"/providers/*.py "$LIB/providers/"
install -o root -g root -m 644 "$HERE/rjob-autostop.service" "$HERE/rjob-autostop.timer" "$UNITS/"
sed -e "s|@WORKER@|$WORKER|g" -e "s|@PYTHON@|$PY|g" -e "s|@RUNNER@|$RUNNER|g" -e "s|@ROOT@|$ROOT|g" \
  "$HERE/rjob-boot.service" > "$UNITS/rjob-boot.service"
chmod 644 "$UNITS/rjob-boot.service"
install -d -o root -g root -m 755 /var/lib/rjob-power

systemctl daemon-reload
systemctl enable rjob-boot.service
systemctl start rjob-boot.service      # fresh idle clock now, as after a boot

echo "== provider state (tests the credential)"
STATE=$("$LIB/rjob-autostop" --state) || { echo "provider check failed: fix the credential/profile; timer NOT enabled" >&2; exit 1; }
echo "$STATE"
if [[ "$STATE" != RUNNING ]]; then
  echo "expected RUNNING from the provider, got '$STATE': report it (the state names may differ); timer NOT enabled" >&2
  exit 1
fi
echo "== dry run"
"$LIB/rjob-autostop" --dry-run
systemctl enable --now rjob-autostop.timer
echo "autostop enabled: the host stops after idle_minutes without jobs or activity."
echo "status: systemctl list-timers rjob-autostop.timer; cat /var/lib/rjob-power/status.json"
echo "pause:  systemctl stop rjob-autostop.timer   (or from the VM: ssh <alias> hold <hours>)"
