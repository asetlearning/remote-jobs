# CLAUDE.md — remote-jobs

This repo is the bridge from the sandboxed agent VM to remote compute hosts. Keep this file current.

## Layout

| Path | What |
|---|---|
| `host/run_job.py` | The whole host side: SSH verb dispatch, git mirrors, per-SHA builds, scheduler, supervisor, manifest, fetch. Stdlib only, Python 3.11+ |
| `host/run-job` | The forced-command shim that `authorized_keys` points at |
| `host/install.sh` | Host setup, run as the worker account |
| `client/rjob` | The VM client; stdlib only |
| `examples/` | Job specs: `smoke-device`, `b25-build`, `pb-smoke` |
| `tests/test_runner.py` | End-to-end test with a fake ssh; no real host needed |
| `docs/` | `mac-setup.md`, `execution-policy.md` (local vs remote; why native, not Docker, on the Mac) |

## Invariants (do not break)

1. **Security boundary.**
   - Over SSH, only the verbs in `SSH_VERBS` are reachable, with regex-validated args.
   - Internal verbs (`_supervise`, `_tick`) must stay unreachable over `--ssh`.
   - Never `shell=True`, and never interpolate client input into a shell.
   - Bundles may contain only `refs/rjob/<sha>`, and tar extraction uses `filter="data"`.
2. **Exact provenance.**
   - Jobs reference full SHAs, and the client refuses dirty trees.
   - The build key is a hash of the SHA, the submodule SHAs and the build steps.
   - `provenance.json` fields are a protected format that experiment notes depend on.
3. **The shared build tree is read-only for jobs.** Work dirs are symlink farms, and overlay, data and outputs paths are real files and directories.
4. **Job contract.** `job.json` has the keys `name`, `code{repo,sha,submodules,build_steps?}`, `resources{device,cpus,timeout_hours}`, `run{argv,files,outputs}`, `data` and `runs_dir`. It is protected: a future container backend must honour the same contract.

## Workflow

- Run `python3 tests/test_runner.py` before proposing a merge, and add checks for new behaviour.
- New work goes on a branch named `NN-short-slug`. **Never merge to `main` without the owner's (Alex Myasnikov) approval.**
- After changing `host/run_job.py`, the host must re-run `host/install.sh`, which copies the runner into `~/rjob/bin`.

## Research context

- The vault dependency note is `obsidian/_meta/dependencies/dep-remote-jobs.md` in `asetlearning/math-agents-challenge-generation`.
- The first consumer is `#project/challenge-gen` (PatternBoost on B(2,5)).
