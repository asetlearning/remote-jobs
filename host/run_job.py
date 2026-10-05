#!/usr/bin/env python3
"""run_job.py — host side of remote-jobs.

Runs on the compute host (the Mac now, a Linux server later) as an unprivileged
account. It is reached in two ways:

* Over SSH: the VM's key is locked in authorized_keys to the `run-job` shim
  (`restrict,command=".../run-job"`). The shim calls `run_job.py --ssh`, and the
  client's command line arrives in $SSH_ORIGINAL_COMMAND. It is split with shlex,
  checked against a verb whitelist and argument regexes, and never executed.
* Internally: the scheduler starts detached supervisors as
  `run_job.py _supervise <id>`. Internal verbs are refused on the --ssh path.

Layout under $RJOB_ROOT (default ~/rjob):
    config.toml            slots, tool paths (optional; defaults below)
    mirrors/<repo>.git     bare mirrors; pushed commits live at refs/rjob/<sha>
    builds/<repo>@<key>/   src/ (worktree + submodule worktrees + .venv), build.log, state
    data/<sha256>          content-addressed inputs (put-data)
    jobs/<id>/             job.json, bundle/, work/, state.json, log.txt, provenance.json, manifest.json
    locks/                 flock files

Python 3.11+ standard library only.
"""
from __future__ import annotations

import fnmatch
import fcntl
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

try:
    import tomllib
except ImportError:  # pragma: no cover - host must run Python 3.11+
    tomllib = None

ROOT = Path(os.environ.get("RJOB_ROOT", Path.home() / "rjob")).expanduser()
HERE = Path(__file__).resolve()

DEFAULT_CONFIG = {
    "gpu_slots": 1,
    "cpu_slots": 7,
    "uv": str(Path.home() / ".local/bin/uv"),
    "python": sys.executable,
    "keep_awake": True,
}

RE_REPO = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
RE_SHA = re.compile(r"^[0-9a-f]{40}$")
RE_SHA256 = re.compile(r"^[0-9a-f]{64}$")
RE_ID = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")
RE_INT = re.compile(r"^[0-9]{1,6}$")
RE_RELPATH = re.compile(r"^(?!/)(?!.*(^|/)\.\.(/|$))[A-Za-z0-9._/+=@-]{1,400}$")

TERMINAL = {"done", "failed", "cancelled", "timeout", "dead"}
ACTIVE = {"building", "running"}


class UserError(Exception):
    """Bad request from the client; printed to stderr, exit 2."""


# ---------------------------------------------------------------- utilities

def config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    path = ROOT / "config.toml"
    if path.exists() and tomllib is not None:
        cfg.update(tomllib.loads(path.read_text()))
    return cfg


def ensure_layout() -> None:
    for sub in ("mirrors", "builds", "data", "jobs", "locks"):
        (ROOT / sub).mkdir(parents=True, exist_ok=True)


@contextmanager
def flock(name: str):
    path = ROOT / "locks" / f"{name}.lock"
    with open(path, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def write_json(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True))
    os.replace(tmp, path)


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def killpg(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def run(cmd, log=None, cwd=None, env=None, check=True) -> int:
    """Run a command, appending its output to the open file `log`."""
    if log is not None:
        log.write(f"$ {shlex.join(str(c) for c in cmd)}\n")
        log.flush()
    proc = subprocess.run(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                          stdout=log or subprocess.DEVNULL, stderr=subprocess.STDOUT)
    if check and proc.returncode != 0:
        raise RuntimeError(f"command failed ({proc.returncode}): {shlex.join(str(c) for c in cmd)}")
    return proc.returncode


def git(*args, git_dir: Path | None = None, cwd=None, capture=True) -> str:
    cmd = ["git"]
    if git_dir is not None:
        cmd += ["--git-dir", str(git_dir)]
    cmd += list(args)
    out = subprocess.run(cmd, cwd=cwd, check=True, capture_output=capture, text=True)
    return out.stdout.strip() if capture else ""


def job_dir(job_id: str) -> Path:
    if not RE_ID.match(job_id):
        raise UserError("bad job id")
    d = ROOT / "jobs" / job_id
    if not d.is_dir():
        raise UserError("no such job")
    return d


def job_state(d: Path) -> dict:
    st = read_json(d / "state.json", {"state": "unknown"})
    if st.get("state") in ACTIVE:
        pid = st.get("pid")
        if not pid or not pid_alive(pid):
            st["state"] = "dead"
    return st


def set_state(d: Path, **fields) -> dict:
    with flock(f"state-{d.name}"):
        st = read_json(d / "state.json", {})
        st.update(fields)
        write_json(d / "state.json", st)
    return st


def mirror(repo: str) -> Path:
    if not RE_REPO.match(repo):
        raise UserError("bad repo name")
    return ROOT / "mirrors" / f"{repo}.git"


# --------------------------------------------------------------- code verbs

def cmd_has(repo: str, *shas: str) -> None:
    """Print the subset of `shas` the host does NOT have as commits."""
    m = mirror(repo)
    for sha in shas:
        if not RE_SHA.match(sha):
            raise UserError("bad sha")
        ok = m.exists() and subprocess.run(
            ["git", "--git-dir", str(m), "cat-file", "-e", f"{sha}^{{commit}}"],
            capture_output=True).returncode == 0
        if not ok:
            print(sha)


def cmd_refs(repo: str) -> None:
    """Print the commit SHAs the mirror holds as refs (used as bundle bases)."""
    m = mirror(repo)
    if not m.exists():
        return
    out = git("for-each-ref", "--format=%(objectname)", "refs/rjob/", git_dir=m)
    if out:
        print(out)


def cmd_push(repo: str) -> None:
    """Read a git bundle on stdin and store its refs/rjob/* refs in the mirror."""
    m = mirror(repo)
    with flock(f"mirror-{repo}"):
        if not m.exists():
            git("init", "--bare", "-q", str(m))
        with tempfile.NamedTemporaryFile(dir=ROOT / "locks", suffix=".bundle", delete=False) as tmp:
            shutil.copyfileobj(sys.stdin.buffer, tmp)
            bundle = Path(tmp.name)
        try:
            v = subprocess.run(["git", "--git-dir", str(m), "bundle", "verify", str(bundle)],
                               capture_output=True, text=True)
            if v.returncode != 0:
                raise UserError("bundle verify failed: " + v.stderr.strip().splitlines()[-1])
            heads = git("bundle", "list-heads", str(bundle), git_dir=m).splitlines()
            refs = [line.split()[1] for line in heads if len(line.split()) == 2]
            if not refs or not all(re.match(r"^refs/rjob/[0-9a-f]{40}$", r) for r in refs):
                raise UserError("bundle must contain only refs/rjob/<sha> refs")
            git("fetch", "-q", str(bundle), "refs/rjob/*:refs/rjob/*", git_dir=m)
            print("\n".join(r.rsplit("/", 1)[1] for r in refs))
        finally:
            bundle.unlink(missing_ok=True)


def cmd_put_data(sha: str) -> None:
    """Store stdin as data/<sha256>, after checking the hash."""
    if not RE_SHA256.match(sha):
        raise UserError("bad sha256")
    dest = ROOT / "data" / sha
    if dest.exists():
        print("exists")
        return
    with tempfile.NamedTemporaryFile(dir=ROOT / "data", delete=False) as tmp:
        shutil.copyfileobj(sys.stdin.buffer, tmp)
        tmp_path = Path(tmp.name)
    if sha256_file(tmp_path) != sha:
        tmp_path.unlink()
        raise UserError("sha256 mismatch")
    os.replace(tmp_path, dest)
    os.chmod(dest, 0o444)
    print("stored")


def cmd_has_data(*shas: str) -> None:
    for sha in shas:
        if not RE_SHA256.match(sha):
            raise UserError("bad sha256")
        if not (ROOT / "data" / sha).exists():
            print(sha)


# ------------------------------------------------------------------- builds

def default_build_steps(src: Path) -> list[list[str]]:
    if (src / "pyproject.toml").exists() and (src / "uv.lock").exists():
        # Same recipe as b25's Dockerfile: deps first, then the C++ extension.
        return [["uv", "sync", "--frozen", "--no-install-project", "--group", "build"],
                ["uv", "pip", "install", "-e", ".", "--no-build-isolation"]]
    return []


def build_key(code: dict) -> str:
    spec = json.dumps({"sha": code["sha"], "submodules": code.get("submodules", {}),
                       "steps": code.get("build_steps")}, sort_keys=True)
    return code["sha"][:12] + "-" + hashlib.sha256(spec.encode()).hexdigest()[:8]


def ensure_build(code: dict, log, cpus: int) -> Path:
    """Check out code at the exact SHAs and build it once; return the src dir."""
    repo, sha = code["repo"], code["sha"]
    m = mirror(repo)
    bdir = ROOT / "builds" / f"{repo}@{build_key(code)}"
    src = bdir / "src"
    with flock(f"build-{bdir.name}"):
        state = (bdir / "state").read_text().strip() if (bdir / "state").exists() else ""
        if state == "ok":
            log.write(f"[rjob] reusing build {bdir.name}\n")
            (bdir / "last_used").write_text(now())
            return src
        if bdir.exists():  # failed or interrupted build: start over
            subprocess.run(["git", "--git-dir", str(m), "worktree", "remove", "--force", str(src)],
                           capture_output=True)
            shutil.rmtree(bdir, ignore_errors=True)
            git("worktree", "prune", git_dir=m)
        bdir.mkdir(parents=True)
        (bdir / "state").write_text("building")
        blog = open(bdir / "build.log", "w")
        try:
            log.write(f"[rjob] building {bdir.name} (log also in builds/{bdir.name}/build.log)\n")
            log.flush()
            for out in (log, blog):
                out.write(f"[rjob] checkout {repo}@{sha}\n")
            run(["git", "--git-dir", str(m), "worktree", "add", "--detach", "-f", str(src), sha], blog)
            for path, sub in sorted(code.get("submodules", {}).items()):
                if not RE_RELPATH.match(path):
                    raise UserError(f"bad submodule path {path}")
                sm = mirror(sub["repo"])
                target = src / path
                if target.exists():
                    shutil.rmtree(target)
                blog.write(f"[rjob] submodule {path} <- {sub['repo']}@{sub['sha']}\n")
                run(["git", "--git-dir", str(sm), "worktree", "add", "--detach", "-f",
                     str(target), sub["sha"]], blog)
            steps = code.get("build_steps")
            if steps is None:
                steps = default_build_steps(src)
            env = build_env(src, cpus)
            for step in steps:
                run(resolve_tool(step), blog, cwd=src, env=env)
            (bdir / "state").write_text("ok")
            (bdir / "last_used").write_text(now())
            blog.write("[rjob] build ok\n")
        except Exception as exc:
            (bdir / "state").write_text("failed")
            blog.write(f"[rjob] build FAILED: {exc}\n")
            raise
        finally:
            blog.close()
            log.write((bdir / "build.log").read_text()[-20000:])
            log.flush()
    return src


def resolve_tool(argv: list[str]) -> list[str]:
    if argv and argv[0] == "uv":
        return [config()["uv"]] + argv[1:]
    return list(argv)


def build_env(src: Path | None, cpus: int) -> dict:
    env = dict(os.environ)
    uv_dir = str(Path(config()["uv"]).parent)
    path = [uv_dir, env.get("PATH", "/usr/bin:/bin")]
    if src is not None:
        venv = src / ".venv"
        env["UV_PROJECT_ENVIRONMENT"] = str(venv)
        env["VIRTUAL_ENV"] = str(venv)
        path.insert(0, str(venv / "bin"))
    env["PATH"] = ":".join(path)
    n = str(max(1, cpus))
    for var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                "CMAKE_BUILD_PARALLEL_LEVEL", "RJOB_CPUS"):
        env[var] = n
    env.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
    env["PYTHONUNBUFFERED"] = "1"
    return env


# ---------------------------------------------------------------- work dirs

def link_farm(src: Path, dst: Path, real_paths: set[str]) -> None:
    """Populate dst with symlinks to src's entries, except along `real_paths`.

    A path listed in real_paths (an overlaid file, a data input, the outputs dir)
    gets real parent directories, so the shared build tree is never written to.
    """
    real_dirs = {}
    for p in real_paths:
        parts = Path(p).parts
        for i in range(1, len(parts)):
            real_dirs.setdefault(Path(*parts[:i]), True)
    blocked = {Path(p) for p in real_paths}

    def fill(rel: Path) -> None:
        sdir, ddir = src / rel, dst / rel
        ddir.mkdir(parents=True, exist_ok=True)
        if not sdir.is_dir():
            return
        for entry in sdir.iterdir():
            r = rel / entry.name
            if entry.name == ".git" or r in blocked:
                continue
            if r in real_dirs:
                fill(r)
            elif not (dst / r).exists():
                (dst / r).symlink_to(entry)

    fill(Path("."))
    for d in real_dirs:
        (dst / d).mkdir(parents=True, exist_ok=True)


def prepare_work(d: Path, job: dict, src: Path | None) -> Path:
    work = d / "work"
    if work.exists():
        shutil.rmtree(work)
    outputs = job["run"].get("outputs", "outputs")
    files = job["run"].get("files", [])
    data = job.get("data", {})
    real = set(files) | set(data) | {outputs}
    for p in real:
        if not RE_RELPATH.match(p):
            raise UserError(f"bad path {p}")
    if src is not None:
        link_farm(src, work, real)
    work.mkdir(exist_ok=True)
    bundle = d / "bundle"
    for p in files:
        (work / p).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(bundle / p, work / p)
    for p, ref in data.items():
        sha = ref.removeprefix("sha256:")
        (work / p).parent.mkdir(parents=True, exist_ok=True)
        (work / p).symlink_to(ROOT / "data" / sha)
    out = work / outputs
    if out.is_symlink():
        out.unlink()
    out.mkdir(parents=True, exist_ok=True)
    return work


# ---------------------------------------------------------------- scheduler

def job_needs(job: dict) -> tuple[int, int]:
    res = job.get("resources", {})
    gpu = 1 if res.get("device", "cpu") in ("mps", "cuda", "gpu", "auto") else 0
    return gpu, int(res.get("cpus", 1))


def all_jobs() -> list[Path]:
    return sorted(p for p in (ROOT / "jobs").iterdir() if p.is_dir() and RE_ID.match(p.name))


def tick() -> None:
    """Start queued jobs (FIFO) while GPU and CPU slots allow."""
    cfg = config()
    with flock("scheduler"):
        used_gpu = used_cpu = 0
        queued = []
        for d in all_jobs():
            st = job_state(d)
            job = read_json(d / "job.json", {})
            if st["state"] in ACTIVE:
                g, c = job_needs(job)
                used_gpu += g
                used_cpu += c
            elif st["state"] == "queued":
                queued.append((d, job))
        for d, job in queued:
            g, c = job_needs(job)
            # FIFO, but a smaller job may fill slots a bigger one is waiting for.
            # A job asking for more CPUs than exist still runs, alone.
            if used_gpu + g > cfg["gpu_slots"] or (used_cpu + c > cfg["cpu_slots"] and used_cpu > 0):
                continue
            log = open(d / "log.txt", "ab")
            proc = subprocess.Popen([sys.executable, str(HERE), "_supervise", d.name],
                                    stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                    start_new_session=True, env=dict(os.environ, RJOB_ROOT=str(ROOT)))
            log.close()
            set_state(d, state="building", pid=proc.pid, started=now())
            used_gpu += g
            used_cpu += c


def spawn_tick() -> None:
    subprocess.Popen([sys.executable, str(HERE), "_tick"], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, env=dict(os.environ, RJOB_ROOT=str(ROOT)))


def probe_env(python: str, env: dict) -> dict:
    code = ("import json,sys,platform\n"
            "d={'python':sys.version.split()[0]}\n"
            "try:\n import torch\n d['torch']=torch.__version__\n"
            " d['cuda']=torch.cuda.is_available()\n"
            " d['mps']=bool(getattr(torch.backends,'mps',None) and torch.backends.mps.is_available())\n"
            "except Exception as e:\n d['torch']=None\n"
            "print(json.dumps(d))")
    try:
        out = subprocess.run([python, "-c", code], env=env, capture_output=True, text=True, timeout=120)
        return json.loads(out.stdout.strip().splitlines()[-1])
    except Exception as exc:  # probing must never fail a job
        return {"probe_error": str(exc)}


def supervise(job_id: str) -> None:
    d = ROOT / "jobs" / job_id
    job = read_json(d / "job.json")
    cfg = config()
    _, cpus = job_needs(job)
    log = open(d / "log.txt", "a", buffering=1)
    prov = {"job_id": job_id, "job": job, "submitted": read_json(d / "state.json", {}).get("submitted"),
            "host": platform.node(), "platform": platform.platform(), "machine": platform.machine()}
    exit_code = None
    try:
        code = job.get("code")
        src = None
        if code:
            src = ensure_build(code, log, cpus)
            prov["build"] = f"{code['repo']}@{build_key(code)}"
            prov["code"] = {"repo": code["repo"], "sha": code["sha"],
                            "submodules": code.get("submodules", {})}
            lock = src / "uv.lock"
            if lock.exists():
                prov["uv_lock_sha256"] = sha256_file(lock)
        if (d / "cancel_requested").exists():
            return
        work = prepare_work(d, job, src)
        prov["inputs"] = {p: sha256_file(d / "bundle" / p) for p in job["run"].get("files", [])}
        prov["data"] = job.get("data", {})
        env = build_env(src, cpus)
        env["RJOB_JOB_ID"] = job_id
        env["RJOB_DEVICE"] = job.get("resources", {}).get("device", "cpu")
        python = str(src / ".venv/bin/python") if src and (src / ".venv/bin/python").exists() else cfg["python"]
        prov["runtime"] = probe_env(python, env)
        argv = list(job["run"]["argv"])
        if argv and argv[0] in ("python", "python3"):
            argv[0] = python
        if cfg.get("keep_awake") and shutil.which("caffeinate"):
            argv = ["caffeinate", "-i"] + argv
        timeout = float(job.get("resources", {}).get("timeout_hours", 24)) * 3600
        prov["argv"] = argv
        prov["started"] = now()
        write_json(d / "provenance.json", prov)
        set_state(d, state="running", run_started=now())
        log.write(f"[rjob] run: {shlex.join(argv)}\n")
        # Own process group, so timeout and cancel reach every worker process.
        child = subprocess.Popen(argv, cwd=work, env=env, stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        set_state(d, child_pgid=child.pid)
        try:
            exit_code = child.wait(timeout=timeout)
            final = "done" if exit_code == 0 else "failed"
        except subprocess.TimeoutExpired:
            killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=30)
            except subprocess.TimeoutExpired:
                killpg(child.pid, signal.SIGKILL)
            final = "timeout"
            log.write(f"[rjob] timeout after {timeout:.0f}s\n")
    except Exception as exc:
        log.write(f"[rjob] ERROR: {exc}\n")
        final = "failed"
    finally:
        if (d / "cancel_requested").exists():
            final = "cancelled"
        prov["finished"] = now()
        prov["exit_code"] = exit_code
        prov["state"] = final
        write_json(d / "provenance.json", prov)
        try:
            write_manifest(d, job)
        except Exception as exc:
            log.write(f"[rjob] manifest error: {exc}\n")
        set_state(d, state=final, exit_code=exit_code, finished=now())
        log.write(f"[rjob] {final} exit={exit_code}\n")
        log.close()
        tick()


def outputs_dir(d: Path, job: dict) -> Path:
    return d / "work" / job["run"].get("outputs", "outputs")


def write_manifest(d: Path, job: dict) -> list:
    out = outputs_dir(d, job)
    items = []
    if out.is_dir():
        for p in sorted(out.rglob("*")):
            if p.is_file() and not p.is_symlink():
                items.append({"path": str(p.relative_to(out)), "size": p.stat().st_size,
                              "sha256": sha256_file(p)})
    write_json(d / "manifest.json", items)
    return items


# ---------------------------------------------------------------- job verbs

def validate_job(job: dict) -> None:
    if not isinstance(job.get("run", {}).get("argv"), list) or not job["run"]["argv"]:
        raise UserError("job.run.argv must be a non-empty list")
    code = job.get("code")
    if code:
        mirror(code["repo"])
        if not RE_SHA.match(code.get("sha", "")):
            raise UserError("code.sha must be a full resolved SHA")
        for path, sub in code.get("submodules", {}).items():
            mirror(sub["repo"])
            if not RE_SHA.match(sub.get("sha", "")) or not RE_RELPATH.match(path):
                raise UserError("bad submodule spec")
        steps = code.get("build_steps")
        if steps is not None and not all(isinstance(s, list) and s for s in steps):
            raise UserError("build_steps must be a list of argv lists")
    res = job.get("resources", {})
    if res.get("device", "cpu") not in ("cpu", "mps", "cuda", "gpu", "auto"):
        raise UserError("bad device")
    if int(res.get("cpus", 1)) < 1:
        raise UserError("bad cpus")
    for p, ref in job.get("data", {}).items():
        if not RE_RELPATH.match(p) or not RE_SHA256.match(str(ref).removeprefix("sha256:")):
            raise UserError("bad data spec")
        if not (ROOT / "data" / ref.removeprefix("sha256:")).exists():
            raise UserError(f"data {ref} not on host; run put-data first")


def cmd_submit() -> None:
    """stdin: tar.gz with job.json and the overlay files under bundle paths."""
    job_id = time.strftime("%Y%m%d-%H%M%S-") + os.urandom(3).hex()
    d = ROOT / "jobs" / job_id
    bundle = d / "bundle"
    bundle.mkdir(parents=True)
    try:
        with tarfile.open(fileobj=sys.stdin.buffer, mode="r|gz") as tar:
            tar.extractall(bundle, filter="data")
        job = read_json(bundle / "job.json")
        if job is None:
            raise UserError("bundle needs job.json")
        validate_job(job)
        for p in job["run"].get("files", []):
            if not RE_RELPATH.match(p) or not (bundle / p).is_file():
                raise UserError(f"file {p} missing from bundle")
        if job.get("code"):
            m = mirror(job["code"]["repo"])
            subs = [(mirror(s["repo"]), s["sha"]) for s in job["code"].get("submodules", {}).values()]
            for mm, sha in [(m, job["code"]["sha"])] + subs:
                if subprocess.run(["git", "--git-dir", str(mm), "cat-file", "-e", f"{sha}^{{commit}}"],
                                  capture_output=True).returncode != 0:
                    raise UserError(f"commit {sha[:12]} not on host; push it first")
    except Exception:
        shutil.rmtree(d, ignore_errors=True)
        raise
    write_json(d / "job.json", job)
    write_json(d / "state.json", {"state": "queued", "submitted": now(), "name": job.get("name", "")})
    (d / "log.txt").touch()
    tick()
    print(job_id)


def cmd_status(job_id: str) -> None:
    d = job_dir(job_id)
    st = job_state(d)
    last = ""
    log = d / "log.txt"
    if log.exists():
        with open(log, "rb") as fh:
            fh.seek(max(0, log.stat().st_size - 4096))
            lines = fh.read().decode(errors="replace").strip().splitlines()
            last = lines[-1] if lines else ""
    st["last_log_line"] = last
    print(json.dumps(st, sort_keys=True))


def cmd_logs(job_id: str, n: str = "100") -> None:
    if not RE_INT.match(n):
        raise UserError("bad line count")
    d = job_dir(job_id)
    lines = (d / "log.txt").read_text(errors="replace").splitlines()
    print("\n".join(lines[-int(n):]))


def cmd_list() -> None:
    for d in reversed(all_jobs()):
        st = job_state(d)
        job = read_json(d / "job.json", {})
        print(f"{d.name}\t{st['state']}\t{job.get('resources', {}).get('device', 'cpu')}\t{job.get('name', '')}")


def cmd_cancel(job_id: str) -> None:
    d = job_dir(job_id)
    st = job_state(d)
    if st["state"] in TERMINAL:
        print(st["state"])
        return
    (d / "cancel_requested").touch()
    if st["state"] == "queued":
        set_state(d, state="cancelled", finished=now())
    else:
        for key in ("child_pgid", "pid"):
            if st.get(key):
                killpg(st[key], signal.SIGTERM)
        set_state(d, state="cancelled", finished=now())
    print("cancelled")
    spawn_tick()


def cmd_manifest(job_id: str) -> None:
    d = job_dir(job_id)
    job = read_json(d / "job.json")
    st = job_state(d)
    items = read_json(d / "manifest.json") if st["state"] in TERMINAL else None
    if items is None:
        items = write_manifest(d, job)
    print(json.dumps(items))


def cmd_provenance(job_id: str) -> None:
    print(json.dumps(read_json(job_dir(job_id) / "provenance.json", {}), indent=2))


def cmd_fetch(job_id: str) -> None:
    """stdout: tar.gz of the requested output files (paths on stdin, one per line; none = all)."""
    d = job_dir(job_id)
    out = outputs_dir(d, read_json(d / "job.json"))
    if not out.is_dir():
        raise UserError("job has no outputs yet")
    wanted = [l.strip() for l in sys.stdin.read().splitlines() if l.strip()]
    for p in wanted:
        if not RE_RELPATH.match(p) or not (out / p).is_file():
            raise UserError(f"bad or missing output path {p}")
    with tarfile.open(fileobj=sys.stdout.buffer, mode="w|gz", compresslevel=1) as tar:
        if wanted:
            for p in wanted:
                tar.add(out / p, arcname=p, recursive=False)
        else:
            for p in sorted(out.rglob("*")):
                if p.is_file() and not p.is_symlink():
                    tar.add(p, arcname=str(p.relative_to(out)), recursive=False)


def cmd_info() -> None:
    cfg = config()
    usage = shutil.disk_usage(ROOT)
    mem = None
    if sys.platform == "darwin":
        mem = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout or 0)
    elif Path("/proc/meminfo").exists():
        mem = int(Path("/proc/meminfo").read_text().split()[1]) * 1024
    running = [d.name for d in all_jobs() if job_state(d)["state"] in ACTIVE]
    queued = [d.name for d in all_jobs() if job_state(d)["state"] == "queued"]
    builds = {p.name: (p / "state").read_text().strip() for p in sorted((ROOT / "builds").iterdir())
              if (p / "state").exists()}
    print(json.dumps({"host": platform.node(), "platform": platform.platform(), "cpus": os.cpu_count(),
                      "mem_gb": round(mem / 2**30, 1) if mem else None,
                      "disk_free_gb": round(usage.free / 2**30, 1), "slots": {"gpu": cfg["gpu_slots"], "cpu": cfg["cpu_slots"]},
                      "running": running, "queued": queued, "builds": builds,
                      "default_python": probe_env(cfg["python"], dict(os.environ))}, indent=2))


def cmd_cleanup(target: str, arg: str = "") -> None:
    """cleanup job <id> | cleanup builds <days>  (builds unused for <days>, not in use)."""
    if target == "job":
        d = job_dir(arg)
        if job_state(d)["state"] not in TERMINAL:
            raise UserError("job is not finished")
        shutil.rmtree(d)
        print("removed", arg)
    elif target == "builds":
        if not RE_INT.match(arg):
            raise UserError("usage: cleanup builds <days>")
        cutoff = time.time() - int(arg) * 86400
        in_use = set()
        for d in all_jobs():
            job = read_json(d / "job.json", {})
            if job.get("code") and job_state(d)["state"] not in TERMINAL:
                in_use.add(f"{job['code']['repo']}@{build_key(job['code'])}")
        for b in sorted((ROOT / "builds").iterdir()):
            stamp = b / "last_used"
            t = stamp.stat().st_mtime if stamp.exists() else b.stat().st_mtime
            if b.name in in_use or t > cutoff:
                continue
            repo = b.name.split("@")[0]
            with flock(f"build-{b.name}"):
                shutil.rmtree(b, ignore_errors=True)
                for m in (ROOT / "mirrors").glob("*.git"):
                    subprocess.run(["git", "--git-dir", str(m), "worktree", "prune"], capture_output=True)
            print("removed", b.name, f"({repo})")
    else:
        raise UserError("usage: cleanup job <id> | cleanup builds <days>")


# --------------------------------------------------------------- dispatch

SSH_VERBS = {
    "has": (cmd_has, 2, 50), "refs": (cmd_refs, 1, 1), "push": (cmd_push, 1, 1),
    "put-data": (cmd_put_data, 1, 1), "has-data": (cmd_has_data, 1, 50),
    "submit": (cmd_submit, 0, 0), "status": (cmd_status, 1, 1), "logs": (cmd_logs, 1, 2),
    "list": (cmd_list, 0, 0), "cancel": (cmd_cancel, 1, 1), "manifest": (cmd_manifest, 1, 1),
    "provenance": (cmd_provenance, 1, 1), "fetch": (cmd_fetch, 1, 1), "info": (cmd_info, 0, 0),
    "cleanup": (cmd_cleanup, 1, 2), "tick": (lambda: tick(), 0, 0),
}
USAGE = "usage: " + " | ".join(sorted(SSH_VERBS))


def main(argv: list[str]) -> int:
    ensure_layout()
    if argv and argv[0] == "--ssh":
        try:
            words = shlex.split(os.environ.get("SSH_ORIGINAL_COMMAND", ""))
        except ValueError:  # unbalanced quotes
            words = []
    else:
        if argv and argv[0] == "_supervise":
            supervise(argv[1])
            return 0
        if argv and argv[0] == "_tick":
            tick()
            return 0
        words = argv
    if not words or words[0] not in SSH_VERBS:
        print(USAGE, file=sys.stderr)
        return 2
    fn, lo, hi = SSH_VERBS[words[0]]
    args = words[1:]
    if not lo <= len(args) <= hi:
        print(USAGE, file=sys.stderr)
        return 2
    try:
        fn(*args)
    except UserError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
