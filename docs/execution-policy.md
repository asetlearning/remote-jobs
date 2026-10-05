# Execution policy: local VM vs remote host

## Local (agent VM: 2 CPUs, ~3 GB RAM, no GPU)

- Editing code, compiling, and unit tests (`ctest`, `pytest`).
- Smoke runs: under about 10 minutes and under 2 GB RAM.
- Data analysis of fetched results, plots, and vault notes.
- Cheap checks on small sets, such as certificate re-expansion, abelianization, and ρ/D on up to a few hundred short words.

## Remote (`rjob submit`)

- **Anything that needs a GPU**: model training and sampling, including PatternBoost runs and sweeps.
- **Anything expected to exceed 30 minutes or 2 GB of RAM**, or to need more than 2 cores.
  - Examples: 2.5-reduction in certify mode on large or long-word sets, Todd–Coxeter experiments, and local-search sweeps.
- **Long unattended runs.** Use `rjob wait` in the background and fetch when it finishes.

## Rules

- **Code goes through git.** Commit to an experiment branch; the client refuses dirty trees. Never merge into a repo's `main` without the owner's approval. Pushing to GitHub is not needed, because commits travel as bundles.
- **One job, one directory of results.** Every result lives in `<runs_dir>/<job-id>/` and carries a `provenance.json` with:
  - the repo and submodule SHAs;
  - the `uv.lock` hash;
  - the job spec;
  - input hashes;
  - host, torch and device.

  Experiment notes cite the job id.
- **Size before you fetch.** Fetch summaries first (`--include`) and pull bulky raw outputs only when needed. Results stay on the host until `rjob cleanup job <id>`.

## Backends

| Host | Backend | GPU | Why |
|---|---|---|---|
| Mac (M1, 64 GB) | native: per-SHA worktree + uv venv | MPS | Docker on macOS runs in a Linux VM without Metal, so containers would be CPU-only |
| Linux/CUDA server (future) | container: `docker run --gpus all -v work:/work -w /work <image>` | CUDA (near-native with nvidia-container-toolkit) | Reproducible environment, and scales to clusters |

- **The job contract is the same for both backends:** `argv`, cwd = the work dir, the `outputs` dir, and `provenance.json`. Moving a job to a server changes only `--host`.
- **Container images** should be built on the server or in CI for that architecture, not in the arm64 VM, where cross-building x86 C++ under QEMU is very slow.
- **b25's `Dockerfile` is stale.** It runs `git submodule update` without `.git`, and its compose file forces `linux/amd64`. Fix it when the container backend is built.
