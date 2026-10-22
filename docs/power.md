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

Two scripts do the per-instance work. Each step is explained in the scripts' comments.
Both are safe to re-run: existing resources are reused, and nothing that is already
filled in is overwritten.

1. **On your laptop** (Nebius CLI logged in as a tenant admin; macOS bash is fine):
   ```bash
   bash host/power/nebius-grant-power.sh --tenant-id <tenant> --project-id <project> \
     --instance-name <vm-name> [--expires-at 2027-10-22T00:00:00Z]
   ```
   This creates or reuses:
   - service account `rjob-autostop-<vm>`;
   - group `rjob-power-<vm>`, with the account as a member;
   - an access permit for `compute.instance-power-operator`, scoped to **this instance
     only**, so the account can view, start and stop this one VM and do nothing else;
   - a new authorized key, written as `rjob-autostop-<vm>.json` with mode 600. The file
     is a secret.

   It then prints the next two commands with the ids filled in. Use `--suffix` to pick
   the names; for example, `--suffix cpuserver` reuses an account and group created by
   hand under those names.
2. **Copy the credentials file to the VM** and delete the laptop copy (the printed
   command uses `scp` to `/tmp/`, then `rm -P`).
3. **On the VM, as root**, after installing the Nebius CLI for root
   ([CLI install](https://docs.nebius.com/cli/install)):
   ```bash
   sudo bash host/power/nebius-setup-profile.sh --credentials /tmp/rjob-autostop-<vm>.json \
     --project-id <project> --instance-id <computeinstance-...> [--worker cpuworker]
   ```
   It:
   - moves the key to `/etc/rjob-power/` (root only, mode 600);
   - creates root's CLI profile `rjob-autostop`;
   - checks that the profile reads this VM as `RUNNING`;
   - checks that it can do nothing else (an IAM listing must be denied), and refuses to
     go on if the permit is broader than that;
   - writes `/etc/rjob-power/autostop.toml`.
4. **Bring the runner up to date:** run `sudo -iu <worker> bash host/install.sh`, which
   gives the runner the `_power-*` verbs.
5. **Install the watchdog:** run `sudo bash host/power/install-autostop.sh`. It refuses to
   enable the timer if the provider check fails or reports anything other than `RUNNING`.
   In that case, report the state it printed: the code assumes Nebius's state names are
   `RUNNING`, `STOPPING` and `STOPPED`.
6. **Set a budget alert** in the Nebius console. It's the backstop if the watchdog ever
   fails silently.

For the manual steps behind the scripts, see Nebius's
[authorized keys](https://docs.nebius.com/iam/service-accounts/authorized-keys) and
[CLI without a browser](https://docs.nebius.com/cli/no-browser) pages.

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
