# remote-jobs

Agents work inside a sandboxed VM (2 CPUs, about 3 GB RAM, no GPU). This repo lets them run heavy experiments on stronger hosts: the Mac (M1, MPS GPU) and any SSH-reachable Linux server (CUDA or CPU). Each project lists the hosts it may use, and jobs are placed on them by device and free capacity.

It covers four things:
1. **Ship code** at an exact commit. Submodules are included, and nothing has to be pushed to GitHub. The host builds each commit once and caches the build.
2. **Run jobs** of any kind: training, sweeps, CPU-heavy computations, or one-off scripts.
3. **Monitor** jobs: state, logs, a blocking wait.
4. **Fetch results**, however large. Fetching is resumable and verified by checksum, and provenance is recorded.
5. **Place jobs** across a project's hosts, with GPUs, CPUs and memory allocated from each job's estimates.

```
VM (agents)                                     Host (Mac or Linux server: user gpuworker)
rjob submit job.toml ──ssh (key locked to run-job)──► ~/rjob/mirrors/<repo>.git    (git bundles)
                                                      ~/rjob/builds/<repo>@<key>/  (worktree + .venv, cached)
rjob status|logs|wait|list|cancel|info               ~/rjob/jobs/<id>/            (work/, log, state, provenance)
rjob fetch <id>  ◄── manifest + checksummed tar ───── scheduler: GPU ids, CPU slots, mem_gb; FIFO
  → <runs_dir>/<id>/{outputs/,provenance.json,manifest.json,log.txt}

project rjob.toml: [[hosts]] mac, gpuserver, ...  → submit places the job; later commands use the recorded host
```

## Quick use (from the VM)

```bash
rjob submit examples/pb-smoke            # resolves SHAs, pushes missing commits, queues; prints a job id
rjob status <id>                         # {"state": "queued|building|running|done|failed|cancelled|timeout|dead", ...}
rjob logs <id> 50
rjob wait <id> --interval 60             # exits 0 iff the job finished with state "done"
rjob fetch <id> [--include '*.json']     # → <runs_dir>/<id>/; re-run to resume or verify
rjob list [--all] | rjob info | rjob cancel <id>
rjob hosts                               # capacity, load, cost/h and max_hours of every project host
rjob cost [--since YYYY-MM-DD] [--host H]  # hours and cost of recorded jobs, per job and per host
rjob put-data big_input.json             # upload once per host (content-addressed); submit uploads [data] to the placed host itself
rjob cleanup job <id> | rjob cleanup builds 30
```

## Local runs: `capjob` (memory cap + rjob fallback)

Local runs on the VM go through `capjob`, so that a runaway job is killed instead of the desktop session or the agent terminals. On 2026-10-15/16 an unbounded 6-worker job got Maestri killed by the kernel OOM killer twice.

```bash
capjob [--mem 2G] [--swap 1G] [--cpus 2] [--out DIR] [--rjob JOB.toml] [--no-fallback] -- CMD ...
```

- **The cap.** CMD runs in its own systemd user scope with `MemoryMax`, `MemorySwapMax` and `CPUQuota`, and with `oom_score_adj` 1000. Thread variables (`OMP_NUM_THREADS`, `MKL_…`, `OPENBLAS_…`, `RAYON_…`, `RJOB_CPUS`) default to `--cpus`.
- **The fallback when the job is killed** (SIGKILL: its cap, or a system OOM):
  1. With `--rjob JOB.toml`, that spec is submitted.
  2. Otherwise a `job.toml` is generated under `<project>/jobs/capjob/<name>-<stamp>/`. `<project>` is the nearest `rjob.toml` above the cwd, else `$RJOB_PROJECT`, else `~/Research/challenge-gen`. The spec is submitted automatically when it is safe: CMD runs in a git repo with a clean tree, `--out` names the results directory, and no argument is an absolute path outside the repo.
  3. Otherwise the draft is left there for the user to finish and submit.
- **Exit codes:** the job's own code when it finishes; **75** = killed, draft left (action needed); **76** = killed and submitted (the job id is printed on stdout). `CAPJOB:` lines on stderr are for agents to parse.

## Hosts and projects

Hosts are SSH aliases (`~/.ssh/config`), each with its own key locked to `run-job`. A project lists its hosts in an `rjob.toml` in the project directory, e.g. `~/Research/challenge-gen/rjob.toml` (template: `examples/project-rjob.toml`):

```toml
runs_dir = "runs"                       # project default, relative to this file
[[hosts]]                               # order = preference
alias = "mac"
devices = ["cpu", "mps"]                # optional; otherwise learned from `rjob info`
max_hours = 2                           # optional: longest timeout_hours this host should take
[[hosts]]
alias = "gpuserver"
devices = ["cpu", "cuda"]
cost_per_hour = 2.5                     # optional: price of one host hour, default 0 (free)
```

- **Finding the project file:** `--project <dir|file>`, else `$RJOB_PROJECT`, else `project = "..."` in job.toml, else the nearest `rjob.toml` above job.toml (for other commands: above the current directory).
- **Placing a new job:** `--host`, else `$RJOB_HOST`, else `host = "..."` in job.toml, else the first project host that can run the job's device, has enough GPUs in total and has room now (free GPU ids, CPU slots and memory, empty queue), else the capable host with the shortest queue. Hosts are queried in parallel with a light `info brief`; unreachable ones are skipped. `submit` prints `host: <alias> (<reason>)` on stderr; `--dry-run` places without submitting and reports `host: none` instead of failing when no host can take the job.
- **Hour limits:** a job counts as `resources.timeout_hours` long (default 24, the host's default). `max_hours` is a preference, not a refusal: placement passes over a host whose `max_hours` is smaller while another capable host is reachable (even a busy one, which then queues the job), and says so in the reason. When no other capable host exists or answers (no other GPU host, the only project host, the others down), the limited host takes the job and the reason says so. The project's host list may change at any time; the rule is evaluated per submit. A pinned host (`--host`, `$RJOB_HOST`, `host = ...`) takes the job with a warning.
- **Costs:** `cost_per_hour` (default 0) is the price of one hour on the host. `submit` prints `cost: <= X` (rate × `timeout_hours`, an upper bound) for paid hosts, and the local record keeps the rate. `rjob cost` prices each recorded job by the hours it held its host (start, build included, to finish; to now while running) and sums per host. Each job is charged the full host rate, so concurrent jobs on one host overstate the bill; the hosts' own invoices are the real cost.
- **Without a project file** a new job goes, as before, to `--host`, else `$RJOB_HOST`, else `mac`.
- **Commands on a job id** (`status`, `logs`, `wait`, `fetch`, `cancel`, `provenance`, `manifest`, `cleanup job`) use the host recorded at submit in `~/.local/state/rjob/jobs.jsonl`; `--host` overrides. A job missing from the record goes to `$RJOB_HOST`, else the project host that knows its id, else `mac` (as before multi-host).
- `--host` and `--project` may come before or after the command.

## Job spec (`job.toml`)

```toml
name = "pb-dehn-gated-01"
runs_dir = "~/Research/challenge-gen/runs"   # where `rjob fetch` puts results

[code]                                       # omit for a self-contained script job
path = "~/Research/challenge-gen/b25_pyproject_agentic"   # local checkout; must be clean
# rev = "HEAD"                                # default; the client resolves to a full SHA
# build_steps = [["uv","sync","--frozen"]]    # default for uv projects: deps, then `uv pip install -e .`
[code.submodules."cpp/tcgraph"]              # optional: override the submodule pin
sha = "main"                                 # any rev in the local submodule checkout

[resources]
device = "mps"        # cpu | mps | cuda | gpu (mps or cuda) | auto (GPU if the host has one)
gpus = 1              # GPUs for mps|cuda|gpu|auto jobs (default 1); CUDA hosts expose only these
cpus = 4              # CPU slots; also sets OMP_NUM_THREADS / CMAKE_BUILD_PARALLEL_LEVEL / RJOB_CPUS
mem_gb = 16           # optional memory estimate; schedules against the host's mem_gb budget
timeout_hours = 48

[run]
argv = ["python", "-m", "experiments.pattern_boost.pattern_boost_main", "run", "configs/x.yaml"]
files = ["configs/x.yaml"]     # paths relative to job.toml, overlaid into the work dir
outputs = "output"             # directory under the work dir that is returned

[data]                         # large inputs: uploaded once by sha256, symlinked in
"data/challenges.json" = "~/data/challenges.json"
```

**Rules**
- **Exact commits only.** The client refuses a dirty working tree, so commit experimental code on a branch first. The SHAs that are actually used are recorded in `provenance.json`.
- **The work directory** mirrors the build tree through symlinks, so relative paths such as `configs/` and `data/` keep working. Overlaid files, data and `outputs` are real files and directories. Jobs never modify the shared build.
- **Interpreter.** `python` in `argv` runs the build's `.venv`; for jobs without `[code]` it runs the host's default venv.
- **Environment.** Jobs see `RJOB_JOB_ID`, `RJOB_DEVICE`, `RJOB_CPUS`, `RJOB_GPU_IDS` and `PYTORCH_ENABLE_MPS_FALLBACK=1`. On CUDA hosts `CUDA_VISIBLE_DEVICES` holds the job's GPU ids (empty for CPU jobs), so `cuda:0` is the job's first GPU.
- **Estimates.** `cpus`, `gpus` and `mem_gb` are reservations used by the host scheduler, not enforced limits. A job that asks for more CPUs or memory than the host has runs alone; more GPUs than the host has is refused, and so is `gpus = 0` with a GPU device. A host refuses devices it lacks (`cuda` on the Mac, `mps` on Linux).
- **GPU numbering.** CUDA jobs also get `CUDA_DEVICE_ORDER=PCI_BUS_ID`, so CUDA's numbering matches `nvidia-smi` and the ids the scheduler reserved.

## Why not Docker on the Mac?

Docker on macOS runs containers inside a Linux VM, and Metal/MPS is not passed through to it. A PyTorch job in a container on the Mac would therefore run on the CPU only. Mac jobs instead run natively, as builds pinned to a commit SHA.

Linux servers currently use the same native backend (per-SHA worktree + uv venv). A `container` backend (`docker run --gpus all`) and a Slurm scheduler may come later, behind the same job contract: argv, the work dir, outputs and provenance. Details are in [docs/execution-policy.md](docs/execution-policy.md).

## Setup

- Mac: [docs/mac-setup.md](docs/mac-setup.md) covers the worker account, the locked SSH key and the VM client.
- Linux server: [docs/server-setup.md](docs/server-setup.md).
- Paid cloud host: also [docs/power.md](docs/power.md), so the host stops itself (through the provider API) after 30 idle minutes.
- Then add the host's alias to the project's `rjob.toml`.

## Tests

`python3 tests/test_runner.py` runs the full lifecycle without a real host. A fake `ssh` invokes the forced-command shim exactly as sshd would. The checks cover:
- push, including submodules;
- build caching;
- the overlay;
- the slot queue;
- cancel and timeout;
- data upload;
- resumable fetch;
- provenance;
- rejection of hostile commands;
- multi-host: placement by device and free capacity, spill to the next host, recorded hosts, disjoint GPU ids, memory estimates, and compatibility with runners that predate multi-host;
- autostop (with a fake provider): no stop while jobs run or queue, within the idle window, under a hold or a login; `request-stop`; submits refused while stopping; provider failures; boot restarts queued jobs; the Nebius provider's CLI arguments.

## Security model

- **Locked key.** The VM's key is locked in `authorized_keys` with `restrict,command="~/rjob/bin/run-job"`. Every login runs `run_job.py --ssh`.
- **Command parsing.** The client's command line is parsed against a whitelist of verbs and argument regexes, and is never executed. There is no shell, scp, rsync or forwarding.
- **Code transfer.** Code arrives as git bundles, which must verify and may contain only `refs/rjob/<sha>`.
- **Archive extraction.** Tar extraction uses Python's `data` filter, which rejects absolute paths, `..` and links outside the tree.
- **Autostop (paid hosts).** The root watchdog holds a provider key that may only start and stop its own instance, in a file the worker can't read. It never parses worker-owned files: it asks the runner, run as the worker. The internal `_power-*` verbs are unreachable over SSH. See [docs/power.md](docs/power.md).
- **Residual risk.** Job code is arbitrary and runs as the worker account, which must hold nothing of value (no Apple ID, no keys, no personal files).
