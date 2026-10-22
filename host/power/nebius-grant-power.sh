#!/bin/bash
# nebius-grant-power.sh — grant power operations on one Nebius VM to a service account.
# Run on YOUR LAPTOP, where the Nebius CLI is logged in as a tenant admin.
#
# Two uses:
#   --for host (default)  the cloud VM's own watchdog, so it can stop itself when idle.
#                         Next step: nebius-setup-profile.sh, as root on that VM.
#   --for vm              the agents' VM, so `rjob power start` can start that cloud VM.
#                         One account for all cloud VMs: run once per cloud VM, with
#                         --no-key after the first. Next step: vm/nebius-vm-setup.sh.
#
# It creates (or reuses, when re-run):
#   service account  rjob-autostop-<suffix>   (--for vm: rjob-vm-power[-<suffix>])
#   group            rjob-power-<suffix>      (--for vm: rjob-vm-power[-<suffix>]);
#                                             roles are granted to groups, not accounts
#   membership       the account in the group
#   access permit    compute.instance-power-operator on THIS INSTANCE ONLY
#                    (view / start / stop / view operations; nothing else, no other VM)
#   authorized key   a key pair: the public half is uploaded to Nebius, the private half
#                    is written to a local credentials file (a SECRET); skipped with --no-key
#
#   bash nebius-grant-power.sh --tenant-id ID --project-id ID \
#        (--instance-name NAME | --instance-id ID) [--for host|vm] [--suffix S] \
#        [--expires-at RFC3339] [--no-key] [--out DIR] [--admin-profile PROFILE]
#
#   --tenant-id      tenant that owns the group (Nebius console: tenant settings)
#   --project-id     project of the VM; the service account is created there
#   --instance-name  VM name as shown in the console, or
#   --instance-id    VM id (computeinstance-...)
#   --for            host (default): for the cloud VM's watchdog; vm: for the agents' VM
#   --suffix         names the account and group; default: the instance name (--for host),
#                    none (--for vm)
#   --no-key         grant only; keep using the key made earlier (adding a VM to --for vm)
#   --expires-at     key expiry, e.g. 2027-10-22T00:00:00Z. After it, the VM can no longer
#                    stop itself: it stays up and reports the error (never loses jobs)
#   --out            where to write the credentials file; default: current directory
#   --admin-profile  your CLI profile, if not the active one
#
# Safe to re-run: existing account, group, membership and permit are reused. It never
# overwrites a credentials file; each run without one makes a NEW key (old keys stay
# valid until you delete them: nebius iam auth-public-key list / delete).
# Works with macOS's bash 3.2.
#
# Created by: Alex Myasnikov
# Date: October 22, 2026
set -euo pipefail

die() { echo "error: $*" >&2; exit 1; }
usage() { sed -n '/^#   bash nebius-grant-power.sh/,/^#   --admin-profile/p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }

TENANT_ID="" PROJECT_ID="" INSTANCE_NAME="" INSTANCE_ID="" SUFFIX="" EXPIRES_AT="" OUT="." ADMIN_PROFILE=""
FOR=host NO_KEY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --tenant-id) TENANT_ID="${2:-}"; shift 2 ;;
    --project-id) PROJECT_ID="${2:-}"; shift 2 ;;
    --instance-name) INSTANCE_NAME="${2:-}"; shift 2 ;;
    --instance-id) INSTANCE_ID="${2:-}"; shift 2 ;;
    --suffix) SUFFIX="${2:-}"; shift 2 ;;
    --for) FOR="${2:-}"; shift 2 ;;
    --no-key) NO_KEY=1; shift ;;
    --expires-at) EXPIRES_AT="${2:-}"; shift 2 ;;
    --out) OUT="${2:-}"; shift 2 ;;
    --admin-profile) ADMIN_PROFILE="${2:-}"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "unknown argument: $1" >&2; usage ;;
  esac
done

# Ids and names go straight into CLI arguments: accept only plain identifiers, and never
# anything starting with "-" (it would be read as a flag).
ID_RE='^[a-z0-9][a-z0-9-]{0,99}$'
NAME_RE='^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$'
[ -n "$TENANT_ID" ] && [ -n "$PROJECT_ID" ] || usage
[[ "$TENANT_ID" =~ $ID_RE ]] || die "bad --tenant-id"
[[ "$PROJECT_ID" =~ $ID_RE ]] || die "bad --project-id"
[ -n "$INSTANCE_ID$INSTANCE_NAME" ] || die "give --instance-name or --instance-id"
[ -z "$INSTANCE_ID" ] || [[ "$INSTANCE_ID" =~ $ID_RE ]] || die "bad --instance-id"
[ -z "$INSTANCE_NAME" ] || [[ "$INSTANCE_NAME" =~ $NAME_RE ]] || die "bad --instance-name"
[ -z "$ADMIN_PROFILE" ] || [[ "$ADMIN_PROFILE" =~ $NAME_RE ]] || die "bad --admin-profile"
[ -z "$EXPIRES_AT" ] || [[ "$EXPIRES_AT" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+(Z|[+-][0-9:]+)$ ]] || die "bad --expires-at (RFC 3339, e.g. 2027-10-22T00:00:00Z)"
[ -d "$OUT" ] || die "no such directory: $OUT"
[ "$FOR" = host ] || [ "$FOR" = vm ] || die "--for must be host or vm"
command -v nebius >/dev/null || die "nebius CLI not found on PATH"

# Every call runs as you (the admin), with your profile if given.
nb() {
  if [ -n "$ADMIN_PROFILE" ]; then nebius "$@" --profile "$ADMIN_PROFILE"; else nebius "$@"; fi
}
# Print a resource's id, or nothing if the lookup fails (e.g. it does not exist yet).
id_of() { nb "$@" --format 'jsonpath={.metadata.id}' 2>/dev/null | tr -d '"[:space:]' || true; }
# Run a create; treat "already exists" as success, so re-runs are harmless.
create_or_exists() {
  local err
  if err=$(nb "$@" 2>&1 >/dev/null); then return 0; fi
  if printf '%s' "$err" | grep -qiE 'already.?exists|ALREADY_EXISTS'; then
    echo "   (already exists)"
    return 0
  fi
  printf '%s\n' "$err" >&2
  return 1
}

# 1. The VM. A name is resolved to its id; either way, show what we found so you can
#    check it is the right machine before anything is granted.
echo "== 1. instance"
if [ -z "$INSTANCE_ID" ]; then
  INSTANCE_ID=$(id_of compute instance get-by-name --parent-id "$PROJECT_ID" --name "$INSTANCE_NAME")
  [ -n "$INSTANCE_ID" ] || die "no instance named $INSTANCE_NAME in project $PROJECT_ID"
fi
[[ "$INSTANCE_ID" =~ $ID_RE ]] || die "unexpected instance id: $INSTANCE_ID"
SHOWN=$(nb compute instance get --id "$INSTANCE_ID" --format 'jsonpath={.metadata.name}' | tr -d '"')
echo "   $INSTANCE_ID ($SHOWN)"
if [ "$FOR" = host ]; then
  [ -n "$SUFFIX" ] || SUFFIX="${INSTANCE_NAME:-$SHOWN}"
  SUFFIX=$(printf '%s' "${SUFFIX:-$INSTANCE_ID}" | tr 'A-Z_.' 'a-z--' | tr -cd 'a-z0-9-' | cut -c1-40)
  SA_NAME="rjob-autostop-$SUFFIX"
  GROUP_NAME="rjob-power-$SUFFIX"
else
  # The agents' VM: one account and group for every cloud VM it may start.
  SUFFIX=$(printf '%s' "$SUFFIX" | tr 'A-Z_.' 'a-z--' | tr -cd 'a-z0-9-' | cut -c1-40)
  SA_NAME="rjob-vm-power${SUFFIX:+-$SUFFIX}"
  GROUP_NAME="$SA_NAME"
fi

# 2. The service account: a non-human identity in the VM's project. It has no rights of
#    its own; it gets them through the group in steps 3-4.
echo "== 2. service account $SA_NAME"
SA_ID=$(id_of iam service-account get-by-name --parent-id "$PROJECT_ID" --name "$SA_NAME")
if [ -z "$SA_ID" ]; then
  SA_ID=$(id_of iam service-account create --parent-id "$PROJECT_ID" --name "$SA_NAME")
  [ -n "$SA_ID" ] || die "could not create service account $SA_NAME"
  echo "   created $SA_ID"
else
  echo "   reusing $SA_ID"
fi

# 3. A group with the account as its only member. --for host: one group per VM, so the
#    grant from step 4 reaches no other machine. --for vm: one group holding one permit
#    per cloud VM the agents may start.
echo "== 3. group $GROUP_NAME"
GROUP_ID=$(id_of iam group get-by-name --parent-id "$TENANT_ID" --name "$GROUP_NAME")
if [ -z "$GROUP_ID" ]; then
  GROUP_ID=$(id_of iam group create --parent-id "$TENANT_ID" --name "$GROUP_NAME")
  [ -n "$GROUP_ID" ] || die "could not create group $GROUP_NAME"
  echo "   created $GROUP_ID"
else
  echo "   reusing $GROUP_ID"
fi
create_or_exists iam group-membership create --parent-id "$GROUP_ID" --member-id "$SA_ID"
echo "   member: $SA_ID"

# 4. The permission: role compute.instance-power-operator, granted to the group, on the
#    instance as the resource (not the project). The account can then view, start and
#    stop this one VM, and do nothing else anywhere.
echo "== 4. access permit: compute.instance-power-operator on $INSTANCE_ID"
create_or_exists iam access-permit create --parent-id "$GROUP_ID" --resource-id "$INSTANCE_ID" \
  --role compute.instance-power-operator

# 5. The key: the CLI generates a key pair, uploads the public key to the account and
#    writes the private key plus ids into a JSON credentials file. That file is the
#    secret the VM authenticates with: written with mode 600, moved to the VM in step 6,
#    then deleted here.
if [ "$NO_KEY" = 1 ]; then
  echo "== 5. authorized key: skipped (--no-key); the existing key of $SA_NAME now also covers $INSTANCE_ID"
  if [ "$FOR" = vm ]; then
    printf '\n== Done. On the agents VM, add the host to power.toml:\n     bash vm/nebius-vm-setup.sh --alias <ssh-alias> --project-id %s --instance-id %s\n' \
      "$PROJECT_ID" "$INSTANCE_ID"
  fi
  exit 0
fi
echo "== 5. authorized key"
CRED="$OUT/$SA_NAME.json"
[ ! -e "$CRED" ] || die "$CRED exists: move it to the VM (step 6) or delete it, then re-run"
umask 077
GEN=(iam auth-public-key generate --service-account-id "$SA_ID" --output "$CRED")
[ -z "$EXPIRES_AT" ] || GEN+=(--expires-at "$EXPIRES_AT")
nb "${GEN[@]}" >/dev/null
[ -s "$CRED" ] || die "no credentials file written"
chmod 600 "$CRED"
echo "   wrote $CRED${EXPIRES_AT:+ (expires $EXPIRES_AT)}"

# 6. What to do next, with every value filled in.
if [ "$FOR" = vm ]; then
  cat <<EOF

== Done on the laptop. Next, on the agents' VM (as its usual user, not root):
1. Copy the credentials file (it is a secret), then delete it here:
     scp $CRED <you>@<agents-vm>:/tmp/
     rm -P $CRED        # macOS; on Linux: shred -u $CRED
2. On the agents' VM, from the remote-jobs checkout:
     bash vm/nebius-vm-setup.sh --credentials /tmp/$SA_NAME.json \\
       --project-id $PROJECT_ID --instance-id $INSTANCE_ID --alias <ssh-alias>
EOF
  exit 0
fi
cat <<EOF

== Done on the laptop. Next, on the VM:
1. Copy the credentials file (it is a secret), then delete it here:
     scp $CRED <you>@<vm-address>:/tmp/
     rm -P $CRED        # macOS; on Linux: shred -u $CRED
2. On the VM, as root, from the remote-jobs checkout:
     sudo bash host/power/nebius-setup-profile.sh \\
       --credentials /tmp/$SA_NAME.json \\
       --project-id $PROJECT_ID \\
       --instance-id $INSTANCE_ID
EOF
