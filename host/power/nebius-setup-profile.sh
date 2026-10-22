#!/bin/bash
# nebius-setup-profile.sh — step 2 of 2 to enable autostop on a Nebius VM.
# Run ON THE VM, as root, after nebius-grant-power.sh on your laptop and after the
# Nebius CLI is installed for root (https://docs.nebius.com/cli/install).
# Then run install-autostop.sh, which installs and enables the watchdog.
#
# It:
#   1. moves the credentials file into /etc/rjob-power/ (root only, mode 600) and
#      deletes the copy it was given;
#   2. creates root's CLI profile that authenticates with that file;
#   3. checks the profile can read this VM's state (must be RUNNING);
#   4. checks the profile can do nothing else (an IAM list must be denied);
#   5. fills in /etc/rjob-power/autostop.toml (worker, instance id, profile, CLI path).
#
#   sudo bash nebius-setup-profile.sh --credentials FILE --project-id ID --instance-id ID \
#        [--profile NAME] [--worker USER] [--cli PATH] [--keep-source] [--force-config]
#
#   --credentials   the JSON file from nebius-grant-power.sh (e.g. /tmp/rjob-autostop-x.json)
#   --project-id    the VM's project (the profile's parent)
#   --instance-id   this VM's id (computeinstance-...)
#   --profile       CLI profile name; default rjob-autostop
#   --worker        remote-jobs worker account; default cpuworker
#   --cli           nebius binary; default: on PATH, else /root/.nebius/bin/nebius
#   --keep-source   do not delete the given credentials file
#   --force-config  rewrite autostop.toml even if it is already filled in
#
# Safe to re-run: an existing profile with the same name is left as is (delete it with
# `nebius profile delete <name>` to recreate it), and a filled-in autostop.toml is kept.
#
# Created by: Alex Myasnikov
# Date: October 22, 2026
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ETC="${RJOB_POWER_ETC:-/etc/rjob-power}"    # override only for tests

die() { echo "error: $*" >&2; exit 1; }
usage() { sed -n '/^#   sudo bash nebius-setup-profile.sh/,/^#   --force-config/p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }

CRED="" PROJECT_ID="" INSTANCE_ID="" PROFILE="rjob-autostop" WORKER="cpuworker" CLI="" KEEP=0 FORCE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --credentials) CRED="${2:-}"; shift 2 ;;
    --project-id) PROJECT_ID="${2:-}"; shift 2 ;;
    --instance-id) INSTANCE_ID="${2:-}"; shift 2 ;;
    --profile) PROFILE="${2:-}"; shift 2 ;;
    --worker) WORKER="${2:-}"; shift 2 ;;
    --cli) CLI="${2:-}"; shift 2 ;;
    --keep-source) KEEP=1; shift ;;
    --force-config) FORCE=1; shift ;;
    -h|--help) usage ;;
    *) echo "unknown argument: $1" >&2; usage ;;
  esac
done

# Values end up in CLI arguments and in a TOML file: plain identifiers only, no leading "-".
ID_RE='^[a-z0-9][a-z0-9-]{0,99}$'
NAME_RE='^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$'
[ -n "$CRED" ] && [ -n "$PROJECT_ID" ] && [ -n "$INSTANCE_ID" ] || usage
[[ "$PROJECT_ID" =~ $ID_RE ]] || die "bad --project-id"
[[ "$INSTANCE_ID" =~ $ID_RE ]] || die "bad --instance-id"
[[ "$PROFILE" =~ $NAME_RE ]] || die "bad --profile"
[[ "$WORKER" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || die "bad --worker"
[ -f "$CRED" ] || die "no such file: $CRED"

# Root only: the key must be readable by root alone, never by the worker (job code).
if [ -z "${RJOB_POWER_ETC:-}" ]; then
  [ "$(id -u)" = 0 ] || die "run as root: sudo bash $0 ..."
  OWN=(-o root -g root)
else
  OWN=()
fi
WHOME=$(getent passwd "$WORKER" | cut -d: -f6) || true
[ -n "$WHOME" ] || die "no such user: $WORKER"

if [ -z "$CLI" ]; then
  CLI=$(command -v nebius || true)
  [ -n "$CLI" ] || CLI=/root/.nebius/bin/nebius
fi
[ -x "$CLI" ] || die "nebius CLI not found at $CLI (install it for root, or pass --cli)"
[[ "$CLI" =~ ^/[A-Za-z0-9._/-]+$ ]] || die "--cli must be an absolute plain path"
nb() { "$CLI" "$@" --profile "$PROFILE" --no-check-update --no-progress; }

# 1. Key into place. /etc/rjob-power is root:root 700 and the file 600, so the worker
#    account (and every job it runs) cannot read it. The copy in /tmp is deleted.
echo "== 1. credentials"
install -d "${OWN[@]+"${OWN[@]}"}" -m 700 "$ETC"
DEST="$ETC/$(basename "$CRED")"
if [ "$(readlink -f "$CRED")" != "$(readlink -f "$DEST" 2>/dev/null || true)" ]; then
  install "${OWN[@]+"${OWN[@]}"}" -m 600 "$CRED" "$DEST"
  if [ "$KEEP" = 0 ]; then shred -u "$CRED" 2>/dev/null || rm -f "$CRED"; fi
fi
echo "   $DEST"

# 2. Root's CLI profile: authenticates as the service account with that file, against
#    the VM's project. It lives in /root/.nebius (root only). Creating it also makes it
#    root's active profile, which is fine: root has no other Nebius identity here.
echo "== 2. profile $PROFILE"
if "$CLI" profile list 2>/dev/null | grep -qw -- "$PROFILE"; then
  echo "   exists; left as is (nebius profile delete $PROFILE, then re-run, to recreate)"
else
  "$CLI" profile create --endpoint api.nebius.cloud --service-account-file "$DEST" \
    --parent-id "$PROJECT_ID" --profile "$PROFILE"
fi

# 3. Can it see this VM? The watchdog reads .status.state and acts only on RUNNING.
echo "== 3. instance state"
STATE=$(nb compute instance get --id "$INSTANCE_ID" --format 'jsonpath={.status.state}' | tr -d '"[:space:]')
echo "   $STATE"
[ "$STATE" = RUNNING ] || die "expected RUNNING for this (running) VM, got '$STATE': check the permit, or report the state name"

# 4. Least privilege: anything beyond power operations on this VM must be refused.
#    If this succeeds, the permit was granted too broadly (e.g. on the project).
echo "== 4. least privilege"
if nb iam service-account list --parent-id "$PROJECT_ID" >/dev/null 2>&1; then
  die "the profile can list service accounts: its permission is broader than compute.instance-power-operator on this instance; fix the access permit before enabling autostop"
fi
echo "   IAM listing denied, as it should be"

# 5. autostop.toml: the watchdog's config. Written from the template the first time (or
#    with --force-config); kept if already filled in, so hand edits survive re-runs.
echo "== 5. $ETC/autostop.toml"
CFG="$ETC/autostop.toml"
if [ -f "$CFG" ] && ! grep -q REPLACE-ME "$CFG" && [ "$FORCE" = 0 ]; then
  echo "   already filled in; kept (--force-config rewrites it). Make sure it has:"
  echo "     instance_id = \"$INSTANCE_ID\"  profile = \"$PROFILE\"  cli = \"$CLI\""
else
  sed -e "s|cpuworker|$WORKER|g" -e "s|/home/$WORKER|$WHOME|g" \
      -e "s|^instance_id = .*|instance_id = \"$INSTANCE_ID\"|" \
      -e "s|^profile = .*|profile = \"$PROFILE\"|" \
      -e "s|^cli = .*|cli = \"$CLI\"|" \
      "$HERE/autostop.toml.example" > "$CFG.tmp"
  install "${OWN[@]+"${OWN[@]}"}" -m 600 "$CFG.tmp" "$CFG"
  rm -f "$CFG.tmp"
  echo "   written (worker $WORKER, idle_minutes 30)"
fi

cat <<EOF

== Done. Next, on this VM:
1. As $WORKER, make sure the runner is current:  sudo -iu $WORKER bash $HERE/../install.sh
2. As root, install and enable the watchdog:       sudo bash $HERE/install-autostop.sh
EOF
