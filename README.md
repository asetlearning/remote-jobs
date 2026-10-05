# remote-jobs

Agents work inside a sandboxed VM (2 CPUs, about 3 GB RAM, no GPU). This repo lets them run heavy experiments on a stronger host: first the Mac host (M1, MPS GPU) and later Linux/CUDA servers.

It covers four things:
1. **Ship code** at an exact commit. Submodules are included, and nothing has to be pushed to GitHub. The host builds each commit once and caches the build.
2. **Run jobs** of any kind: training, sweeps, CPU-heavy computations, or one-off scripts.
3. **Monitor** jobs: state, logs, a blocking wait.
4. **Fetch results**, however large. Fetching is resumable and verified by checksum, and provenance is recorded.

```
VM (agents)                                     Host (Mac: user gpuworker; later a server)
rjob submit job.toml ──ssh (key locked to run-job)──► ~/rjob/mirrors/<repo>.git    (git bundles)
                                                      ~/rjob/builds/<repo>@<key>/  (worktree + .venv, cached)
rjob status|logs|wait|list|cancel|info               ~/rjob/jobs/<id>/            (work/, log, state, provenance)
rjob fetch <id>  ◄── manifest + checksummed tar ───── scheduler: 1 GPU slot, N CPU slots, FIFO
  → <runs_dir>/<id>/{outputs/,provenance.json,manifest.json,log.txt}
```

## Quick use (from the VM)

```bash
rjob submit examples/pb-smoke            # resolves SHAs, pushes missing commits, queues; prints a job id
rjob status <id>                         # {"state": "queued|building|running|done|failed|cancelled|timeout|dead", ...}
rjob logs <id> 50
rjob wait <id> --interval 60             # exits 0 iff the job finished with state "done"
rjob fetch <id> [--include '*.json']     # → <runs_dir>/<id>/; re-run to resume or verify
rjob list | rjob info | rjob cancel <id>
rjob put-data big_input.json             # upload once (content-addressed); reference it in [data]
rjob cleanup job <id> | rjob cleanup builds 30
```

Hosts are SSH aliases. Pass `--host`, or set `$RJOB_HOST`; the default is `mac`.

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
device = "mps"        # cpu | mps | cuda | auto   (anything but cpu takes the GPU slot)
cpus = 4              # CPU slots; also sets OMP_NUM_THREADS / CMAKE_BUILD_PARALLEL_LEVEL / RJOB_CPUS
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
- **Environment.** Jobs see `RJOB_JOB_ID`, `RJOB_DEVICE`, `RJOB_CPUS` and `PYTORCH_ENABLE_MPS_FALLBACK=1`.

## Why not Docker on the Mac?

Docker on macOS runs containers inside a Linux VM, and Metal/MPS is not passed through to it. A PyTorch job in a container on the Mac would therefore run on the CPU only. Mac jobs instead run natively, as builds pinned to a commit SHA.

Linux/CUDA servers will get a `container` backend (`docker run --gpus all`, with images built on the server or in CI). It will sit behind the same job contract: argv, the work dir, outputs and provenance. Details are in [docs/execution-policy.md](docs/execution-policy.md).

## Setup

See [docs/mac-setup.md](docs/mac-setup.md), which covers the Mac worker account, the locked SSH key and the VM client.

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
- rejection of hostile commands.

## Security model

- **Locked key.** The VM's key is locked in `authorized_keys` with `restrict,command="~/rjob/bin/run-job"`. Every login runs `run_job.py --ssh`.
- **Command parsing.** The client's command line is parsed against a whitelist of verbs and argument regexes, and is never executed. There is no shell, scp, rsync or forwarding.
- **Code transfer.** Code arrives as git bundles, which must verify and may contain only `refs/rjob/<sha>`.
- **Archive extraction.** Tar extraction uses Python's `data` filter, which rejects absolute paths, `..` and links outside the tree.
- **Residual risk.** Job code is arbitrary and runs as the worker account, which must hold nothing of value (no Apple ID, no keys, no personal files).
