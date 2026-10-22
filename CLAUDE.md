# CLAUDE.md — remote-jobs

This repo is the bridge from the sandboxed agent VM to remote compute hosts. Keep this file current.

## Layout

| Path | What |
|---|---|
| `host/run_job.py` | The whole host side: SSH verb dispatch, git mirrors, per-SHA builds, scheduler (GPU ids, CPU slots, mem_gb), supervisor, manifest, fetch. Stdlib only, Python 3.11+ |
| `host/run-job` | The forced-command shim that `authorized_keys` points at |
| `host/install.sh` | Host setup, run as the worker account |
| `host/power/` | Autostop for paid cloud hosts: `rjob-autostop` (root, systemd timer), `providers/` (`nebius`, `fake`), units, `install-autostop.sh` (root), per-instance Nebius setup `nebius-grant-power.sh` (laptop, admin) and `nebius-setup-profile.sh` (VM, root). See `docs/power.md` |
| `client/rjob` | The VM client: project host lists (`rjob.toml`, with optional `cost_per_hour` and `max_hours` per host), placement, recorded hosts, `rjob cost`; stdlib only |
| `vm/nebius-vm-setup.sh` | Agents' VM: Nebius key and profile for `rjob power`, `~/.config/rjob/power.toml` |
| `client/capjob` | Local runs on the VM inside a memory/CPU cap (systemd user scope, oom_score_adj 1000); when the cap kills the job, it falls back to `rjob submit`. Stdlib only |
| `examples/` | Job specs: `smoke-device`, `smoke-device-cuda`, `b25-build`, `pb-smoke`; `project-rjob.toml` (per-project host list) |
| `tests/test_runner.py` | End-to-end test with a fake ssh; no real host needed |
| `tests/test_capjob.py` | capjob: cap, exit codes, fallback paths, with a stub rjob (needs a systemd user session) |
| `docs/` | `mac-setup.md`, `server-setup.md` (Linux/CUDA), `power.md` (autostop on paid hosts), `execution-policy.md` (local vs remote; backends) |

## Invariants (do not break)

1. **Security boundary.**
   - Over SSH, only the verbs in `SSH_VERBS` are reachable, with regex-validated args.
   - Internal verbs (`_supervise`, `_tick`, `_power-check`, `_power-abort`, `_power-boot`) must stay unreachable over `--ssh`.
   - The root watchdog (`host/power/rjob-autostop`) never parses worker-owned files or runs worker code as root; it asks the runner through `runuser` as the worker. Its provider key may only start and stop its own instance.
   - The client calls only a provider's `state` and `start`; stops go through the runner's `request-stop` (`rjob power stop`), never a provider `stop` from the VM.
   - Only `submit` (and the explicit `rjob power start`, `fetch --start`) starts a host; other commands never do.
   - Autostop never stops a busy host: `_power-check` decides under `flock("scheduler")` and sets `power/stopping`, which `cmd_submit` checks under the same lock.
   - Never `shell=True`, and never interpolate client input into a shell.
   - Bundles may contain only `refs/rjob/<sha>`, and tar extraction uses `filter="data"`.
2. **Exact provenance.**
   - Jobs reference full SHAs, and the client refuses dirty trees.
   - The build key is a hash of the SHA, the submodule SHAs and the build steps.
   - `provenance.json` fields are a protected format that experiment notes depend on.
3. **The shared build tree is read-only for jobs.** Work dirs are symlink farms, and overlay, data and outputs paths are real files and directories.
4. **Job contract.** `job.json` has the keys `name`, `code{repo,sha,submodules,build_steps?}`, `resources{device,cpus,timeout_hours,gpus?,mem_gb?}`, `run{argv,files,outputs}`, `data` and `runs_dir`. It is protected: a future container or Slurm backend must honour the same contract. Changes are additive only.
5. **Mixed versions.** The client must work with hosts that run an older `run_job.py` (hosts update only when their owner re-runs `install.sh`): new `info` fields are optional on the client side, new job fields optional on the host side.

## Workflow

- Run `python3 tests/test_runner.py` and `python3 tests/test_capjob.py` before proposing a merge, and add checks for new behaviour.
- New work goes on a branch named `NN-short-slug`. **Never merge to `main` without the owner's (Alex Myasnikov) approval.**
- After changing `host/run_job.py`, the host must re-run `host/install.sh`, which copies the runner into `~/rjob/bin`.

## Research context

- The vault dependency note is `obsidian/_meta/dependencies/dep-remote-jobs.md` in `asetlearning/math-agents-challenge-generation`.
- The first consumer is `#project/challenge-gen` (PatternBoost on B(2,5)).
