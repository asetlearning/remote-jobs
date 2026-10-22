#!/bin/bash
# nebius-vm-setup.sh — let `rjob power start` start a Nebius VM from the agents' VM.
# Run ON THE AGENTS' VM, as its usual user (not root), after
#   nebius-grant-power.sh --for vm ...   on your laptop (it made the key file), and
#   the Nebius CLI install for this user  (https://docs.nebius.com/cli/install).
#
# It:
#   1. moves the credentials file into ~/.config/rjob/power/ (mode 600, dir 700) and
#      deletes the copy it was given (first run only; later runs reuse the profile);
#   2. creates this user's CLI profile that authenticates with that file;
#   3. checks the profile can read the cloud VM's state;
#   4. checks the profile can do nothing else (an IAM list must be denied);
#   5. adds or updates [hosts.<alias>] in ~/.config/rjob/power.toml (mode 600).
#
#   bash nebius-vm-setup.sh --alias ALIAS --project-id ID --instance-id ID \
#        [--credentials FILE] [--profile NAME] [--cli PATH] [--keep-source]
#
#   --alias        the cloud VM's ssh alias, as in ~/.ssh/config and the project's rjob.toml
#   --project-id   the cloud VM's project (the profile's parent)
#   --instance-id  the cloud VM's id (computeinstance-...)
#   --credentials  the JSON file from nebius-grant-power.sh --for vm; needed only the first
#                  time (when the profile does not exist yet)
#   --profile      CLI profile name; default rjob-vm-power
#   --cli          nebius binary; default: on PATH, else ~/.nebius/bin/nebius
#   --keep-source  do not delete the given credentials file
#
# Risk accepted by the owner (docs/power.md): agents on this VM can read this key. It can
# only view, start and stop the listed cloud VMs. Agents use `rjob power`, never the
# nebius CLI directly; `rjob power` never stops a busy host.
#
# Created by: Alex Myasnikov
# Date: October 22, 2026
set -euo pipefail
CONF_DIR="${RJOB_POWER_DIR:-$HOME/.config/rjob}"   # override only for tests

die() { echo "error: $*" >&2; exit 1; }
usage() { sed -n '/^#   bash nebius-vm-setup.sh/,/^#   --keep-source/p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }

ALIAS="" PROJECT_ID="" INSTANCE_ID="" CRED="" PROFILE="rjob-vm-power" CLI="" KEEP=0
while [ $# -gt 0 ]; do
  case "$1" in
    --alias) ALIAS="${2:-}"; shift 2 ;;
    --project-id) PROJECT_ID="${2:-}"; shift 2 ;;
    --instance-id) INSTANCE_ID="${2:-}"; shift 2 ;;
    --credentials) CRED="${2:-}"; shift 2 ;;
    --profile) PROFILE="${2:-}"; shift 2 ;;
    --cli) CLI="${2:-}"; shift 2 ;;
    --keep-source) KEEP=1; shift ;;
    -h|--help) usage ;;
    *) echo "unknown argument: $1" >&2; usage ;;
  esac
done

# Values end up in CLI arguments and in a TOML file: plain identifiers only, no leading "-".
ID_RE='^[a-z0-9][a-z0-9-]{0,99}$'
NAME_RE='^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$'
[ -n "$ALIAS" ] && [ -n "$PROJECT_ID" ] && [ -n "$INSTANCE_ID" ] || usage
[[ "$ALIAS" =~ $NAME_RE ]] || die "bad --alias"
[[ "$PROJECT_ID" =~ $ID_RE ]] || die "bad --project-id"
[[ "$INSTANCE_ID" =~ $ID_RE ]] || die "bad --instance-id"
[[ "$PROFILE" =~ $NAME_RE ]] || die "bad --profile"
[ "$(id -u)" != 0 ] || die "run as the agents' usual user, not root"
if [ -z "$CLI" ]; then
  CLI=$(command -v nebius || true)
  [ -n "$CLI" ] || CLI="$HOME/.nebius/bin/nebius"
fi
[ -x "$CLI" ] || die "nebius CLI not found at $CLI (install it, or pass --cli)"
[[ "$CLI" =~ ^/[A-Za-z0-9._/-]+$ ]] || die "--cli must be an absolute plain path"
nb() { "$CLI" "$@" --profile "$PROFILE" --no-check-update --no-progress; }

# 1-2. Key and profile. The key lives next to power.toml, both readable by this user only.
#      A second cloud VM reuses the same profile (the grant added a permit to the same group).
echo "== 1-2. key and profile $PROFILE"
install -d -m 700 "$CONF_DIR" "$CONF_DIR/power"
if "$CLI" profile list 2>/dev/null | grep -qw -- "$PROFILE"; then
  echo "   profile exists; reused"
  if [ -n "$CRED" ]; then echo "   (--credentials ignored; nebius profile delete $PROFILE to switch keys)"; fi
else
  [ -n "$CRED" ] || die "no profile $PROFILE yet: pass --credentials (the file from nebius-grant-power.sh --for vm)"
  [ -f "$CRED" ] || die "no such file: $CRED"
  DEST="$CONF_DIR/power/$(basename "$CRED")"
  install -m 600 "$CRED" "$DEST"
  if [ "$KEEP" = 0 ]; then shred -u "$CRED" 2>/dev/null || rm -f "$CRED"; fi
  "$CLI" profile create --endpoint api.nebius.cloud --service-account-file "$DEST" \
    --parent-id "$PROJECT_ID" --profile "$PROFILE"
  echo "   key $DEST"
fi

# 3. Can it see the cloud VM? Any state is fine here (it may be stopped).
echo "== 3. cloud VM state"
STATE=$(nb compute instance get --id "$INSTANCE_ID" --format 'jsonpath={.status.state}' | tr -d '"[:space:]')
[ -n "$STATE" ] || die "no state for $INSTANCE_ID: check the access permit"
echo "   $STATE"

# 4. Least privilege: anything beyond power operations on the listed VMs must be refused.
echo "== 4. least privilege"
if nb iam service-account list --parent-id "$PROJECT_ID" >/dev/null 2>&1; then
  die "the profile can list service accounts: its permission is broader than compute.instance-power-operator on the listed VMs; fix the access permit"
fi
echo "   IAM listing denied, as it should be"

# 5. power.toml: one [hosts.<alias>] table per cloud VM. Rewritten from the parsed file,
#    so other hosts' entries are kept and this alias is added or updated.
echo "== 5. $CONF_DIR/power.toml"
umask 077
python3 - "$CONF_DIR/power.toml" "$ALIAS" "$INSTANCE_ID" "$PROFILE" "$CLI" <<'EOF'
import json, os, sys, tomllib
path, alias, iid, profile, cli = sys.argv[1:]
cfg = tomllib.load(open(path, "rb")) if os.path.exists(path) else {}
hosts = cfg.get("hosts", {})
hosts[alias] = {"provider": "nebius", "nebius": {"instance_id": iid, "profile": profile, "cli": cli}}
out = ["# rjob power: cloud hosts rjob may start (written by vm/nebius-vm-setup.sh; keep mode 600)"]
for name, h in hosts.items():
    out.append(f"\n[hosts.{json.dumps(name)}]")
    tables = {k: v for k, v in h.items() if isinstance(v, dict)}
    out += [f"{k} = {json.dumps(v)}" for k, v in h.items() if not isinstance(v, dict)]
    for t, vals in tables.items():
        out.append(f"[hosts.{json.dumps(name)}.{t}]")
        out += [f"{k} = {json.dumps(v)}" for k, v in vals.items()]
tmp = path + ".tmp"
with open(tmp, "w") as fh:
    fh.write("\n".join(out) + "\n")
os.chmod(tmp, 0o600)
os.replace(tmp, path)
print(f"   [hosts.{alias}] -> {iid}")
EOF

cat <<EOF

== Done. Check it:  rjob power status $ALIAS
Start the cloud VM: rjob power start $ALIAS   (it stops itself again after its idle window)
EOF
