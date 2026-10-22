# Power: paid hosts stop themselves when idle

A rented cloud VM bills compute for every hour it runs, busy or idle. On a paid host,
`rjob-autostop` stops the VM through the provider's API once the runner has been idle
for `idle_minutes` (default 30). Disks persist while it is stopped: builds, mirrors,
data and job results are all still there after the next start.

- **Stopping** is decided on the host, by itself (below).
- **Starting** is done from the agents' VM with `rjob power start <alias>`, using a key
  that may only power-operate the listed cloud VMs (see "Starting from the VM").
- **Next:** `rjob submit` will start a stopped host on demand; that is part 3 of issue #10.

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
withdraws it. From the VM: `rjob power hold <alias> <hours>` and
`rjob power stop <alias> [--cancel]`.

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

## Starting from the VM: `rjob power`

| Command | What it does |
|---|---|
| `rjob power status [ALIAS]` | Shows the cloud state (provider API), cost per hour, running/queued jobs, the last autostop decision, hold, planned stop time and any stop request, for every host in `power.toml`. |
| `rjob power start ALIAS` | Starts a `STOPPED` host and waits up to 10 min (`--timeout`) until its runner answers; `--no-wait` returns at once. A running host is left alone. A lock per alias stops two agents from starting it twice, and a host that is still `STOPPING` is started once the stop has finished. |
| `rjob power stop ALIAS [--cancel]` | Asks the host to stop once it is idle (`request-stop`). The client never calls the provider's stop, so a busy host is never stopped through rjob. |
| `rjob power hold ALIAS HOURS` | Keeps the host up (≤ 12 h; `0` clears it). Use it before a series of jobs or a long fetch session. |

- **Stopped hosts are reported, not started.** Any other command that cannot reach a host
  listed in `power.toml` says it may be stopped and names the two commands above. Only
  `rjob power start` starts a host for now; automatic starts on submit come in part 3.
- **Config.** `~/.config/rjob/power.toml` (`$RJOB_POWER_CONFIG`) must be mode 600; rjob
  refuses it otherwise. It holds one `[hosts.<alias>]` table per cloud VM: `provider`,
  plus `instance_id`, `profile` and `cli` in `[hosts.<alias>.nebius]`.
  `vm/nebius-vm-setup.sh` writes it.
- **The VM's key and the accepted risk.** This was the owner's choice over a start gate
  on the Mac.
  - The key lives in `~/.config/rjob/power/`, mode 600, and is used through a CLI profile
    (default `rjob-vm-power`).
  - Agents on the VM can read it. It can only view, start and stop the listed cloud VMs.
  - The worst misuse is a raw stop that kills running jobs, or a start that the host's
    autostop undoes 30 idle minutes later.
  - Agents use `rjob power`, never the `nebius` CLI directly.
  - Set a key expiry, and watch the Nebius audit log.

### Setup on the agents' VM (once, then once per extra cloud VM)

1. **On your laptop**, for the first cloud VM:
   ```bash
   bash host/power/nebius-grant-power.sh --for vm --tenant-id <tenant> --project-id <project> \
     --instance-name <vm-name> [--expires-at 2027-10-22T00:00:00Z]
   ```
   This creates (or reuses) the account and group `rjob-vm-power`, a permit for
   `compute.instance-power-operator` on that instance, and a key file.
   For **each further cloud VM**, run the same command with `--no-key`. That adds a permit
   for the new instance to the same group, and the existing key covers it.
2. **Copy the key file to the agents' VM** (`/tmp/`) and delete the laptop copy.
3. **On the agents' VM**, as its usual user, after installing the Nebius CLI
   ([CLI install](https://docs.nebius.com/cli/install); linux/arm64 is supported):
   ```bash
   bash vm/nebius-vm-setup.sh --alias cpuserver --project-id <project> \
     --instance-id <computeinstance-...> --credentials /tmp/rjob-vm-power.json
   ```
   - It moves the key into `~/.config/rjob/power/`, creates the profile and checks the
     VM's state.
   - It refuses a key that can do more than power operations.
   - It writes `[hosts.cpuserver]` into `power.toml`.
   - For further cloud VMs, run it again without `--credentials`.
4. **Check:** `rjob power status`.

## Operating

- **Last decision:** `rjob --host <alias> info` → `power.autostop.decision`, or on the
  host `cat /var/lib/rjob-power/status.json`.
- **Overview:** `rjob power status`.
- **Pause:** `rjob power hold <alias> <hours>` (`0` clears it), or on the host
  `sudo systemctl stop rjob-autostop.timer`. Resume with
  `sudo systemctl start rjob-autostop.timer`.
- **Start:** `rjob power start <alias>`, or the Nebius console.
- **Stop now (once idle):** `rjob power stop <alias>`.
- **Remove:** `sudo bash host/power/install-autostop.sh --uninstall`. It keeps
  `/etc/rjob-power`.

## Other providers

`host/power/providers/<name>.py` exposes `state/start/stop(cfg)`. A new cloud needs one
module (for example `aws ec2 start-instances` with an IAM policy scoped to the instance's
ARN) and a `[<name>]` table in `autostop.toml` (host) or `power.toml` (VM). The VM
client loads the same modules from `host/power/providers/`. Nothing else changes.
