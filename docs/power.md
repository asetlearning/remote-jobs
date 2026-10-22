# Power: paid hosts stop themselves when idle

A rented cloud VM bills compute for every hour it runs, busy or idle. On a paid host,
`rjob-autostop` stops the VM through the provider's API once the runner has been idle
for `idle_minutes` (default 30). Disks persist while it is stopped: builds, mirrors,
data and job results are all still there after the next start.

Phase 1 (this doc): the host stops itself; you start it again from the provider's
console or CLI. Later phases add `rjob power start|status|stop|hold` on the VM and
let `rjob submit` start a stopped host on demand (issue #10).

## Why it works this way

- **Not `shutdown` from inside.** Nebius treats a shutdown inside the guest as a
  failure: it reboots the VM and keeps billing. Only an API or console stop ends
  compute charges. So the watchdog calls the API.
- **The decision is made on the server.** Idleness comes from the runner's own state,
  taken under its scheduler lock. While the lock is held, the runner sets
  `~/rjob/power/stopping`, so a submit is either queued before the check (and the host
  stays up) or refused after it (`host is stopping`). A busy host is never stopped.
- **It doesn't depend on the Mac or the VM.** A sleeping laptop can't leave a server
  running, because the server decides by itself.
- **Narrow credential.** The watchdog runs as root and holds a key that may only start
  and stop this one instance. Job code runs as the worker account and can't read it.
  Root never parses worker-owned files: the runner's verdict comes from running it as
  the worker (`runuser`).
- **Failures leave the host running.** If the API call fails, the watchdog clears the
  marker, records the error and retries in 5 minutes. That costs money but loses no
  jobs. The last line of defence is a provider budget alert.

## When the host stops

All of these must hold at a check (every 5 minutes; the first check comes 10 minutes
after boot):

| Condition | Source |
|---|---|
| No job running, building or queued | the runner (`_power-check`) |
| Nothing happened for `idle_minutes`: no runner command and no job change since then | `~/rjob/power/last_activity`, job `state.json` times, boot time |
| Nobody logged in interactively | `who` |
| No unexpired hold | `hold <hours>` (at most 12 h; `hold 0` clears it) |

Checks that don't count as activity: `info`, `status` and `list`. So `rjob hosts` and
`rjob wait` don't keep a host awake. Fetching results does count.

**`request-stop`** (refused on hosts without autostop, such as the Mac) asks for a stop as soon as the host is idle. It skips the idle
window and any hold, but still waits for running and queued jobs. `request-stop cancel`
withdraws it. Phase 1 has no client command for these verbs yet: from the VM, run
`ssh <alias> hold 2` or `ssh <alias> request-stop` (the locked key only reaches the runner).
`rjob power` comes in phase 2.

**After a start,** `rjob-boot.service` clears the flags, restarts the idle clock and
starts jobs that were still queued when the host went down. Jobs that were running when
the host was stopped some other way (from the console, say) show as `dead`; resubmit
them.

## Setup on a Nebius host (owner; needs Nebius admin and root on the host)

1. **Service account and its permission** (Nebius console, IAM):
   - Create a service account, e.g. `rjob-autostop-<host>`.
   - Create a group, e.g. `rjob-power-<host>`, and add the service account to it.
   - Give the group an access permit with role **`compute.instance-power-operator`**,
     scoped to **this instance only** (resource = the VM, not the project). That role
     can view, start and stop VMs and view operations, nothing else.
2. **Key and CLI profile** (on the host, as root):
   - Install the Nebius CLI for root. See the
     [CLI install docs](https://docs.nebius.com/cli/install).
   - Create an authorized key for the service account and a CLI profile named
     `rjob-autostop` that uses it. See
     [Authenticating as a service account](https://docs.nebius.com/iam/service-accounts/authorized-keys).
     Store the credentials file in `/etc/rjob-power/`, owned by root with mode 600.
   - Check it: `nebius compute instance get --id <instance-id> --profile rjob-autostop`.
3. **Runner up to date:** as the worker, re-run `host/install.sh`, which gives the
   runner the `_power-*` verbs.
4. **Install** (as root):
   ```bash
   sudo bash /path/to/remote-jobs/host/power/install-autostop.sh   # first run writes /etc/rjob-power/autostop.toml
   sudoedit /etc/rjob-power/autostop.toml                           # worker, instance_id, profile
   sudo bash /path/to/remote-jobs/host/power/install-autostop.sh   # installs, tests the credential, enables the timer
   ```
   The installer refuses to enable the timer if the provider check fails, or if the provider reports anything
   other than `RUNNING` for this (running) host. If that happens, report the state it printed: the code assumes
   Nebius's state names are `RUNNING`, `STOPPING` and `STOPPED`.
5. **Budget alert** in the Nebius console. It's the backstop if the watchdog ever
   fails silently.

## Operating

- **Last decision:** `rjob --host <alias> info` → `power.autostop.decision`, or on the
  host `cat /var/lib/rjob-power/status.json`.
- **Pause:** from the VM `ssh <alias> hold <hours>` (`hold 0` clears it; `ssh <alias> request-stop` stops
  once idle), or on the host `sudo systemctl stop rjob-autostop.timer`.
  Resume with `sudo systemctl start rjob-autostop.timer`.
- **Start:** in the Nebius console, or
  `nebius compute instance start --id <instance-id>` with an account allowed to.
- **Remove:** `sudo bash host/power/install-autostop.sh --uninstall`. It keeps
  `/etc/rjob-power`.

## Other providers

`host/power/providers/<name>.py` exposes `state/start/stop(cfg)`. A new cloud needs one
module (for example `aws ec2 start-instances` with an IAM policy scoped to the instance's
ARN) and a `[<name>]` table in `autostop.toml`. Nothing else changes.
