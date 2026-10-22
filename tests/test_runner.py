#!/usr/bin/env python3
"""End-to-end test of client/rjob + host/run_job.py without a real SSH host.

A fake `ssh` runs the forced-command shim exactly as sshd would: the client's
command goes into $SSH_ORIGINAL_COMMAND and HOME points at a scratch host home.
Covers push (incl. submodule), build reuse, submit, queue/slots, wait, fetch
(+ resume, --include), provenance, dirty-tree refusal, cancel, timeout, data
upload and hostile commands; then multi-host: project file, placement by device
and free capacity, recorded hosts, GPU id assignment and memory estimates.
Each ssh alias gets its own host home (home-<alias>).  Run: python3 tests/test_runner.py
"""
# Created by: Alex Myasnikov
# Date: October 5, 2026
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="rjob-test-"))
HOSTHOME = TMP / "home-testhost"
RJOB = [sys.executable, str(REPO / "client/rjob"), "--host", "testhost"]
FAILS = []
OLD_RUNNER_REV = "47b2e83"   # last runner before multi-host (#4); compatibility target


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def sh(*cmd, cwd=None, check_rc=True, env=None, inp=None):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env, input=inp)
    if check_rc and p.returncode != 0:
        raise RuntimeError(f"{cmd}: {p.returncode}\n{p.stdout}\n{p.stderr}")
    return p


def setup_host(home=HOSTHOME, extra="", runner=None):
    b = home / "rjob/bin"
    b.mkdir(parents=True)
    if runner is None:
        shutil.copy(REPO / "host/run_job.py", b)
    else:
        (b / "run_job.py").write_text(runner)
    shutil.copy(REPO / "host/run-job", b)
    (b / "python3").symlink_to(sys.executable)
    base = 'gpu_slots = 1\ncpu_slots = 2\nkeep_awake = false\nuv = "/nonexistent/uv"\n'
    keys = {line.split("=")[0].strip() for line in extra.splitlines()}
    base = "".join(l + "\n" for l in base.splitlines() if l.split("=")[0].strip() not in keys)
    (home / "rjob/config.toml").write_text(base + extra)
    fake = TMP / "fakessh"
    fake.write_text(f'#!/bin/bash\n# fake ssh: $1 = host alias, $2 = command string\n'
                    f'H="{TMP}/home-$1"\n[ -x "$H/rjob/bin/run-job" ] || {{ echo "ssh: unreachable $1" >&2; exit 255; }}\n'
                    f'SSH_ORIGINAL_COMMAND="$2" HOME="$H" exec "$H/rjob/bin/run-job"\n')
    fake.chmod(0o755)
    os.environ["RJOB_SSH"] = str(fake)
    os.environ["RJOB_STATE"] = str(TMP / "state")


def host(cmd: str):
    env = dict(os.environ, SSH_ORIGINAL_COMMAND=cmd, HOME=str(HOSTHOME))
    return subprocess.run([str(HOSTHOME / "rjob/bin/run-job")], env=env, capture_output=True, text=True, input="")


def rjob(*args, check_rc=True):
    return sh(*RJOB, *args, check_rc=check_rc)


def git(repo, *args):
    return sh("git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
              "-c", "protocol.file.allow=always", *args).stdout.strip()


def setup_repos():
    sub = TMP / "subrepo"
    sub.mkdir()
    git(sub, "init", "-q", "-b", "main")
    (sub / "lib.txt").write_text("lib-v1\n")
    git(sub, "add", ".")
    git(sub, "commit", "-qm", "sub v1")
    main = TMP / "mainrepo"
    main.mkdir()
    git(main, "init", "-q", "-b", "main")
    (main / "main.py").write_text(
        "import json, os, pathlib\n"
        "cfg = json.load(open('configs/c.json'))\n"
        "lib = open('ext/sub/lib.txt').read().strip()\n"
        "built = open('built.txt').read()\n"
        "os.makedirs('output/sub', exist_ok=True)\n"
        "open('output/result.txt','w').write(f\"{cfg['msg']} {lib} {built} {os.environ['OMP_NUM_THREADS']}\")\n"
        "open('output/sub/big.bin','wb').write(os.urandom(300000))\n"
        "open('output/summary.json','w').write('{}')\n")
    (main / "configs").mkdir()
    (main / "configs/c.json").write_text('{"msg": "repo-config"}')
    git(main, "add", ".")
    git(main, "commit", "-qm", "main v1")
    git(main, "submodule", "add", "-q", str(sub), "ext/sub")
    git(main, "commit", "-qm", "add submodule")
    return main, sub


def write_spec(d: Path, text: str, files: dict | None = None) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    (d / "job.toml").write_text(text)
    for name, body in (files or {}).items():
        p = d / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return d / "job.toml"


def wait_state(job_id, states, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = json.loads(rjob("status", job_id).stdout)
        if st["state"] in states:
            return st
        time.sleep(0.3)
    return st


def load_client():
    from importlib.machinery import SourceFileLoader
    from importlib.util import module_from_spec, spec_from_loader
    spec = spec_from_loader("rjob_client", SourceFileLoader("rjob_client", str(REPO / "client/rjob")))
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def multi_host():
    """Two hosts: macx (mps, 1 GPU, 2 CPU slots) and gpux (cuda, GPUs 0+1, 4 CPU slots, 10 GB)."""
    setup_host(TMP / "home-macx", 'devices = ["cpu", "mps"]\n')
    setup_host(TMP / "home-gpux", 'devices = ["cpu", "cuda"]\ngpu_ids = [0, 1]\ncpu_slots = 4\nmem_gb = 10\n')
    proj = TMP / "proj"
    proj.mkdir()
    (proj / "rjob.toml").write_text('runs_dir = "runs"\n[[hosts]]\nalias = "macx"\n[[hosts]]\nalias = "gpux"\n'
                                     '[[hosts]]\nalias = "deadhost"\n')
    bare = [sys.executable, str(REPO / "client/rjob")]

    def rj(*args, check_rc=True, cwd=None):
        return sh(*bare, *args, check_rc=check_rc, cwd=cwd)

    sleeper = ("import os, time\nos.makedirs('outputs', exist_ok=True)\n"
               "print('ORDER=' + os.environ.get('CUDA_DEVICE_ORDER', '<unset>'), flush=True)\n"
               "print('CVD=' + os.environ.get('CUDA_VISIBLE_DEVICES', '<unset>'), flush=True)\n"
               "time.sleep(float(open('t').read()))\n")

    def spec(name, res, t="30", extra=""):
        return write_spec(proj / "jobs" / name, f'name = "{name}"\n{extra}[resources]\n{res}\n'
                          '[run]\nargv = ["python", "main.py"]\nfiles = ["main.py", "t"]\n',
                          {"main.py": sleeper, "t": t})

    def host_of(r):
        return r.stderr.split("host: ", 1)[1].split()[0] if "host: " in r.stderr else None

    def cvd(jid, timeout=20):
        t0 = time.time()
        while time.time() - t0 < timeout:
            out = rj("logs", jid).stdout
            if "CVD=" in out:
                return out.split("CVD=", 1)[1].split()[0] if out.split("CVD=", 1)[1].strip() else ""
            time.sleep(0.3)
        return None

    # placement by device
    check(host_of(rj("submit", str(spec("p-cuda", 'device = "cuda"')), "--dry-run")) == "gpux", "cuda job placed on cuda host")
    check(host_of(rj("submit", str(spec("p-mps", 'device = "mps"')), "--dry-run")) == "macx", "mps job placed on mps host")
    check(host_of(rj("submit", str(spec("p-cpu", "cpus = 1")), "--dry-run")) == "macx", "cpu job placed on first host")
    r = rj("submit", str(spec("p-mps-gpux", 'device = "mps"')), "--host", "gpux", check_rc=False)
    check(r.returncode != 0 and "not available" in r.stderr, "host rejects a device it lacks")
    r = rj("submit", str(spec("p-3gpu", 'device = "cuda"\ngpus = 3')), "--host", "gpux", check_rc=False)
    check(r.returncode != 0 and "GPUs" in r.stderr, "host rejects more GPUs than it has")
    r = rj("submit", str(spec("p-0gpu", 'device = "cuda"\ngpus = 0')), "--host", "gpux", check_rc=False)
    check(r.returncode != 0 and "gpus >= 1" in r.stderr, "host rejects gpus = 0 for a GPU device")
    r = rj("submit", str(spec("p-3gpu-place", 'device = "cuda"\ngpus = 3')), "--dry-run")
    check("host: none" in r.stderr and "only 2 GPUs" in r.stderr, "placement skips hosts with too few GPUs in total")
    r = rj("submit", str(spec("p-3gpu-place", 'device = "cuda"\ngpus = 3')), check_rc=False)
    check(r.returncode != 0 and "no project host" in r.stderr, "real submit fails when no host can ever run the job")
    r = sh(*bare, "submit", str(spec("p-env", "cpus = 1")), "--dry-run", env=dict(os.environ, RJOB_HOST="gpux"))
    check(host_of(r) == "gpux", "$RJOB_HOST outranks the project file")
    dead = proj / "dead.toml"
    dead.write_text('[[hosts]]\nalias = "deadhost"\n[[hosts]]\nalias = "deadhost2"\n')
    r = rj("--project", str(dead), "submit", str(spec("p-dead", "cpus = 1")), "--dry-run", check_rc=False)
    check(r.returncode == 0 and "host: none" in r.stderr and "unreachable" in r.stderr,
          "dry run with every host down reports instead of failing")
    check(host_of(rj("submit", str(spec("p-pin", "cpus = 1", extra='host = "gpux"\n')), "--dry-run")) == "gpux",
          "job.toml host pins placement")

    # per-host max_hours and cost_per_hour (project file only; hosts unchanged)
    cost = proj / "cost.toml"
    cost.write_text('runs_dir = "runs"\n[[hosts]]\nalias = "macx"\nmax_hours = 2\n'
                    '[[hosts]]\nalias = "gpux"\ncost_per_hour = 1.5\n')
    cp = ("--project", str(cost))
    check(host_of(rj(*cp, "submit", str(spec("h-short", "cpus = 1\ntimeout_hours = 2")), "--dry-run")) == "macx",
          "job within max_hours stays on the first host")
    r = rj(*cp, "submit", str(spec("h-long", "cpus = 1\ntimeout_hours = 3")), "--dry-run")
    check(host_of(r) == "gpux" and "macx: max_hours 2 < timeout_hours 3" in r.stderr,
          "job longer than max_hours skips that host, with the reason")
    check(host_of(rj(*cp, "submit", str(spec("h-default", "cpus = 1")), "--dry-run")) == "gpux",
          "no timeout_hours counts as the host default (24 h)")
    r = rj(*cp, "submit", str(spec("h-mps", 'device = "mps"\ntimeout_hours = 3')), "--dry-run")
    check(host_of(r) == "macx" and "no capable host within max_hours" in r.stderr,
          "long mps job with no other mps host still goes to the limited host, with the reason")
    alt = proj / "alt.toml"
    alt.write_text('[[hosts]]\nalias = "macx"\nmax_hours = 2\n[[hosts]]\nalias = "deadhost"\n')
    r = rj("--project", str(alt), "submit", str(spec("h-alt-dead", "cpus = 1\ntimeout_hours = 3")), "--dry-run")
    check(host_of(r) == "macx" and "deadhost: unreachable" in r.stderr,
          "unreachable alternative: the limited host takes the long job")
    r = rj(*cp, "submit", str(spec("h-pin", "cpus = 1\ntimeout_hours = 3")), "--host", "macx", "--dry-run")
    check(host_of(r) == "macx" and "warning: macx is pinned although max_hours 2" in r.stderr,
          "--host overrides max_hours, with a warning")
    r = rj(*cp, "submit", str(spec("h-cost", "cpus = 1\ntimeout_hours = 3")), "--dry-run")
    check("cost: <= 4.50 (1.5/h x timeout_hours 3)" in r.stderr, "submit prints the cost bound")
    check("cost:" not in rj(*cp, "submit", str(spec("h-free", "cpus = 1\ntimeout_hours = 1")), "--dry-run").stderr,
          "free hosts print no cost line")
    one = proj / "one.toml"
    one.write_text('[[hosts]]\nalias = "macx"\nmax_hours = 2\n')
    r = rj("--project", str(one), "submit", str(spec("h-one", "cpus = 1\ntimeout_hours = 3")), "--dry-run")
    check(host_of(r) == "macx" and "max_hours 2 < timeout_hours 3, no other host" in r.stderr,
          "single-host project: the only host takes a long job, with the reason")
    check("warning" not in r.stderr, "no pin warning when the host was not pinned")
    for bad in ("cost_per_hour = -1", 'cost_per_hour = "free"', "max_hours = 0"):
        one.write_text(f'[[hosts]]\nalias = "macx"\n{bad}\n')
        r = rj("--project", str(one), "submit", str(spec("h-bad", "cpus = 1")), "--dry-run", check_rc=False)
        check(r.returncode != 0 and "must be a number" in r.stderr, f"bad host field rejected: {bad}")
    lines = {l.split("\t")[0]: l.split("\t") for l in rj(*cp, "hosts").stdout.splitlines()}
    check(lines["gpux"][-2:] == ["1.5", "-"] and lines["macx"][-2:] == ["0", "2"], "rjob hosts shows cost/h and max_hours")
    paid = rj(*cp, "submit", str(spec("h-paid", "cpus = 1\ntimeout_hours = 3", t="1"))).stdout.strip()
    wait_state_any(rj, paid, {"done"})
    rec_paid = [json.loads(l) for l in (TMP / "state/jobs.jsonl").read_text().splitlines() if paid in l][0]
    check(rec_paid["cost_per_hour"] == 1.5, "record keeps the cost rate")
    out = rj("cost", "--host", "gpux").stdout
    row = next(l.split("\t") for l in out.splitlines() if l.startswith(paid))
    check(row[3] == "done" and row[5] == "1.5" and 0 <= float(row[6]) < 0.01, f"rjob cost prices a finished job {row}")
    check(any(l.startswith("total\tgpux\t") for l in out.splitlines()), "rjob cost totals per host")
    check(len(rj("cost", "--since", "2099-01-01").stdout.splitlines()) == 1, "rjob cost --since filters")
    rj("cleanup", "job", paid)
    cl = load_client()
    check(cl.busy_hours({"started": "2026-10-21T10:00:00+0000", "finished": "2026-10-21T11:30:00+0000"}) == 1.5,
          "busy hours from the host's timestamps")
    check(cl.busy_hours({"started": "yesterday"}) is None and cl.busy_hours({}) is None,
          "unparsable or missing start leaves a job unpriced")

    # spill: fill macx's CPU slots, next CPU job goes to gpux; runs_dir from project
    full = rj("submit", str(spec("fill-mac", "cpus = 2"))).stdout.strip()
    wait_state_any(rj, full, {"running"})
    r = rj("submit", str(spec("spill", "cpus = 1")))
    spill = r.stdout.strip()
    check(host_of(r) == "gpux", f"placement spills to the next host when the first is full ({host_of(r)})")
    check(cvd(spill) == "", "cpu job on a cuda host sees no GPUs")
    rec = load_client()
    check(json.loads(rj("status", spill).stdout)["state"] in ("building", "running"), "status resolves the recorded host")
    r = sh(*bare, "status", spill, cwd=proj, env=dict(os.environ, RJOB_STATE=str(TMP / "empty-state")))
    check(r.returncode == 0 and json.loads(r.stdout)["state"] in ("building", "running"),
          "job missing from the record is found on the project host that knows it")
    r = sh(*bare, "status", spill, cwd=TMP, check_rc=False,
           env={**{k: v for k, v in os.environ.items() if k != "RJOB_HOST"}, "RJOB_STATE": str(TMP / "empty-state")})
    check(r.returncode != 0 and "unreachable mac" in r.stderr, "no record, no project: falls back to mac as before")
    r = sh(*bare, "status", "20990101-000000-abcdef", check_rc=False, cwd=proj,
           env={k: v for k, v in os.environ.items() if k != "RJOB_HOST"})
    check(r.returncode != 0 and "no project host knows it" in r.stderr, "unknown job id asks for --host")
    rec_runs = [json.loads(l) for l in (TMP / "state/jobs.jsonl").read_text().splitlines() if spill in l][0]
    check(rec_runs["host"] == "gpux" and rec_runs["job"]["runs_dir"] == str(proj / "runs"),
          "record keeps host; runs_dir defaults from project file")
    for j in (full, spill):
        rj("cancel", j)

    # GPU ids: two cuda jobs get disjoint GPUs, a third waits
    g = [rj("submit", str(spec(f"g{i}", 'device = "cuda"\ncpus = 1'))).stdout.strip() for i in range(3)]
    ids = [cvd(j) for j in g[:2]]
    check(sorted(ids) == ["0", "1"], f"concurrent cuda jobs get disjoint CUDA_VISIBLE_DEVICES {ids}")
    time.sleep(1)
    check(json.loads(rj("status", g[2]).stdout)["state"] == "queued", "third cuda job waits for a GPU")
    prov = json.loads(rj("provenance", g[0]).stdout)
    check(prov.get("gpu_ids") in ([0], [1]), "provenance records gpu ids")
    check("ORDER=PCI_BUS_ID" in rj("logs", g[0]).stdout, "CUDA jobs get CUDA_DEVICE_ORDER=PCI_BUS_ID")
    hosts = rj("hosts", cwd=proj).stdout
    check("gpux" in hosts and "0/2" in hosts and "deadhost\tunreachable" in hosts, "rjob hosts shows load and unreachable hosts")
    check(sum(l.startswith("gpux\t") for l in rj("--project", str(proj / "rjob.toml"), "list", "--all").stdout.splitlines()) == 4, "list --all prefixes hosts")
    rj("cancel", g[0])
    check(wait_state_any(rj, g[2], {"running"})["state"] == "running", "queued cuda job starts when a GPU frees")
    check(cvd(g[2]) == ids[0], "freed GPU id is reused")
    for j in g[1:]:
        rj("cancel", j)

    # memory estimates on gpux (10 GB)
    m = [rj("submit", str(spec(f"m{i}", "cpus = 1\nmem_gb = 6")), "--host", "gpux").stdout.strip() for i in range(2)]
    wait_state_any(rj, m[0], {"running"})
    time.sleep(1)
    check(json.loads(rj("status", m[1]).stdout)["state"] == "queued", "mem_gb blocks co-scheduling")
    for j in m:
        rj("cancel", j)
    big = rj("submit", str(spec("mbig", "cpus = 1\nmem_gb = 64")), "--host", "gpux").stdout.strip()
    check(wait_state_any(rj, big, {"running"})["state"] == "running", "oversize mem job runs alone")
    rj("cancel", big)

    # compat: no project file, no --host -> $RJOB_HOST (else mac)
    lone = write_spec(TMP / "lone", 'name = "lone"\n[run]\nargv = ["python", "x.py"]\n')
    r = sh(*bare, "submit", str(lone), "--dry-run", env=dict(os.environ, RJOB_HOST="testhost"))
    check(host_of(r) == "testhost", "no project: $RJOB_HOST as before")
    r = sh(*bare, "submit", str(lone), "--dry-run", env={k: v for k, v in os.environ.items() if k != "RJOB_HOST"})
    check(host_of(r) == "mac", "no project, no $RJOB_HOST: mac as before")
    r = rj("--project", str(proj), "submit", str(lone), "--dry-run")
    check(host_of(r) == "macx", "--project applies to a spec outside the project dir")

    # a real pre-multi-host runner: `info brief` falls back to full info
    old = sh("git", "-C", str(REPO), "show", f"{OLD_RUNNER_REV}:host/run_job.py").stdout
    setup_host(TMP / "home-oldhost", "", runner=old)
    oi = rec.host_info("oldhost")
    check(oi is not None and "default_python" in oi and "free" not in oi, "old runner: info brief falls back to full info")
    bi = rec.host_info("gpux")
    check(bi is not None and "devices" in bi and "default_python" not in bi and "idle" in bi, "new runner: brief info")
    pj = proj / "old.toml"
    pj.write_text('[[hosts]]\nalias = "oldhost"\n[[hosts]]\nalias = "deadhost"\n')
    r = rj("--project", str(pj), "submit", str(spec("p-old", "cpus = 1")), "--dry-run")
    check(host_of(r) == "oldhost", "placement uses an idle old runner")

    # oversize memory on an idle host fits, whatever the rounding
    idle = {"free": {"gpu": 0, "cpu": 4, "mem_gb": 13.25}, "slots": {"gpu": 0, "cpu": 4, "mem_gb": 13.25},
            "idle": True, "queue_len": 0}
    check(rec.fits_now(idle, {"cpus": 1, "mem_gb": 20}) is True, "oversize mem job fits an idle host")
    check(rec.fits_now(dict(idle, idle=False), {"cpus": 1, "mem_gb": 20}) is False, "oversize mem job waits on a busy host")

    # installer resource detection, with a BSD-style seq that would add a trailing separator
    stub = TMP / "stub"
    stub.mkdir()
    for name, body in {"uname": '[[ "$1" == -m ]] && echo x86_64 || echo Linux',
                       "nvidia-smi": 'echo "GPU 0: A"; echo "GPU 1: B"',
                       "seq": 'echo "0, 1, "'}.items():
        (stub / name).write_text("#!/bin/bash\n" + body + "\n")
        (stub / name).chmod(0o755)
    env = dict(os.environ, PATH=f"{stub}:/usr/bin:/bin", HOME=str(TMP / "nohome"))
    r = sh("bash", str(REPO / "host/install.sh"), "--print-config", env=env, check_rc=False)
    check(r.returncode == 0 and "gpu_ids = [0, 1]" in r.stdout and not (TMP / "nohome").exists(),
          "install.sh --print-config detects GPUs and installs nothing")
    r = sh("bash", str(REPO / "host/install.sh"), "--print-config", env=dict(env, RJOB_GPU_IDS="0;x"), check_rc=False)
    check(r.returncode != 0 and "bad resources" in r.stderr, "install.sh rejects a malformed override")

    # placement against a runner that predates multi-host (no devices/free in info)
    rec.host_info = lambda h: {"default_python": {"mps": h == "old", "cuda": False}, "running": [],
                               "queued": [], "slots": {"gpu": 1, "cpu": 2}}
    pr = {"hosts": [{"alias": "busy"}, {"alias": "old"}]}
    check(rec.place(pr, {"device": "mps"})[0] == "old", "old-style info: device from default_python")
    check(rec.place(pr, {"device": "cpu"})[0] == "busy", "old-style info: idle host is used")


def autostop():
    """Server autostop: host/power/rjob-autostop with the fake provider against a test host `pw`."""
    import getpass
    import signal
    home = TMP / "home-pw"
    setup_host(home, "cpu_slots = 2\nmem_gb = 10\n")
    os.environ["RJOB_AUTOSTOP_STATUS"] = str(TMP / "pw-status.json")
    r = sh(os.environ["RJOB_SSH"], "pw", "request-stop", check_rc=False)
    check(r.returncode == 2 and "no autostop" in r.stderr, "request-stop refused on a host without autostop")
    os.environ["RJOB_AUTOSTOP_BIN"] = str(REPO / "host/power/rjob-autostop")
    fake_state = TMP / "pw-fake.json"
    cfg = TMP / "autostop.toml"

    def write_cfg(logins='["true"]', fail="false"):
        cfg.write_text(f'worker = "{getpass.getuser()}"\nrunner_python = "{sys.executable}"\n'
                       f'runner = "{home}/rjob/bin/run_job.py"\nrjob_root = "{home}/rjob"\n'
                       f'idle_minutes = 0.05\nlogins_cmd = {logins}\nprovider = "fake"\n'
                       f'[fake]\nstate_file = "{fake_state}"\nfail = {fail}\n')
    write_cfg()
    bare = [sys.executable, str(REPO / "client/rjob"), "--host", "pw"]

    def rj(*args, check_rc=True):
        return sh(*bare, *args, check_rc=check_rc)

    def wd(*args):
        r = sh(sys.executable, str(REPO / "host/power/rjob-autostop"), *args, check_rc=False,
               env=dict(os.environ, RJOB_AUTOSTOP_CONFIG=str(cfg)))
        return r.returncode, json.loads(r.stdout) if r.stdout.strip().startswith("{") else {"raw": r.stdout + r.stderr}

    def runner(*args):
        return sh(sys.executable, str(home / "rjob/bin/run_job.py"), *args,
                  env=dict(os.environ, RJOB_ROOT=str(home / "rjob")))

    def verb(cmd, check_rc=True):  # runner verbs without a client command yet (rjob power: phase 2)
        return sh(os.environ["RJOB_SSH"], "pw", cmd, check_rc=check_rc)

    def calls():
        return json.loads(fake_state.read_text())["calls"] if fake_state.exists() else []

    def boot():
        runner("_power-boot")
        st = json.loads(fake_state.read_text()) if fake_state.exists() else {"calls": []}
        fake_state.write_text(json.dumps({**st, "state": "RUNNING"}))

    stopping = home / "rjob/power/stopping"
    idle = lambda: time.sleep(3.3)  # past idle_minutes = 0.05
    sleeper = "import os, time\nos.makedirs('outputs', exist_ok=True)\ntime.sleep(float(open('t').read()))\n"

    def job(name, t, res="cpus = 1"):
        spec = write_spec(TMP / "pwjobs" / name, f'name = "{name}"\n[resources]\n{res}\n[run]\n'
                          'argv = ["python", "main.py"]\nfiles = ["main.py", "t"]\n', {"main.py": sleeper, "t": str(t)})
        return rj("submit", str(spec), check_rc=False)

    j = job("as-busy", 4).stdout.strip()
    wait_state_any(rj, j, {"running"})
    rc, st = wd()
    check(st.get("decision", "").startswith("wait: busy") and not calls(), f"autostop waits while a job runs ({st.get('decision')})")
    wait_state_any(rj, j, {"done"})
    rc, st = wd()
    check(st.get("decision", "").startswith("wait: idle") and not calls(), "autostop waits for the idle window after a job")
    idle()
    rj("info")
    rj("status", j)
    rj("list")
    rc, st = wd()
    check(rc == 0 and st.get("decision", "").startswith("stop sent") and calls() == ["stop"],
          f"idle host stops through the provider; info/status/list are not activity ({st.get('decision')})")
    check(stopping.exists(), "stopping marker set before the stop")
    r = job("as-late", 0)
    check(r.returncode != 0 and "host is stopping" in r.stderr, "submit refused while the host is stopping")
    pw = json.loads(rj("info").stdout)["power"]
    check(pw["stopping"] and pw["autostop"]["decision"].startswith("stop sent"), "info reports the power state and last decision")
    idle()  # the refused submit was runner activity
    rc, st = wd()
    check(st.get("decision") == "no action: instance is STOPPED" and calls() == ["stop"], "no second stop for a stopped instance")
    boot()
    check(not stopping.exists(), "boot clears the stopping marker")

    check(verb("hold 1").returncode == 0, "hold accepted")
    idle()
    rc, st = wd()
    check(st.get("decision", "").startswith("wait: hold") and calls() == ["stop"], "hold keeps an idle host up")
    check(verb("hold 13", check_rc=False).returncode != 0, "hold over 12 h rejected")
    check(verb("hold 1;id", check_rc=False).returncode != 0, "malformed hold rejected")
    verb("request-stop")
    rc, st = wd()
    check(st.get("decision") == "stop sent: stop requested" and calls() == ["stop", "stop"], "request-stop overrides a hold")
    boot()
    verb("hold 0")
    check(not (home / "rjob/power/stop_requested").exists(), "boot clears the stop request")

    idle()
    rj("logs", j)
    rc, st = wd()
    check(st.get("decision", "").startswith("wait: idle"), "a runner command restarts the idle clock")

    write_cfg(logins='["echo", "someone pts/0"]')
    idle()
    rc, st = wd()
    check(st.get("decision", "").startswith("wait: 1 interactive login") and not stopping.exists(),
          "no stop while someone is logged in")
    write_cfg(fail="true")
    rc, st = wd()
    check(rc == 1 and st.get("error") and st.get("decision") == "error: host stays up" and not stopping.exists(),
          "provider failure: host stays up and accepts jobs")
    write_cfg()
    rc, st = wd("--dry-run")
    check(st.get("decision", "").startswith("would stop") and calls() == ["stop", "stop"] and not stopping.exists(),
          "dry run decides but never stops")

    j = job("as-drain", 3).stdout.strip()
    wait_state_any(rj, j, {"running"})
    verb("request-stop")
    rc, st = wd()
    check(st.get("decision", "").startswith("wait: busy") and calls() == ["stop", "stop"], "request-stop never stops a busy host")
    wait_state_any(rj, j, {"done"})
    rc, st = wd()
    check(st.get("decision") == "stop sent: stop requested", "requested stop happens once the host is idle, without the idle window")
    boot()

    # jobs queued when the host went down (raw stop) start at boot
    a = job("as-a", 60, "cpus = 1\nmem_gb = 6").stdout.strip()
    wait_state_any(rj, a, {"running"})
    b = job("as-b", 0, "cpus = 1\nmem_gb = 6").stdout.strip()
    check(json.loads(rj("status", b).stdout)["state"] == "queued", "second job queued behind the memory budget")
    sa = json.loads(rj("status", a).stdout)
    for pid in (sa.get("child_pgid"), sa.get("pid")):
        if pid:
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    time.sleep(0.5)
    check(json.loads(rj("status", a).stdout)["state"] == "dead", "killed job shows dead, like a raw stop")
    time.sleep(1)
    check(json.loads(rj("status", b).stdout)["state"] == "queued", "nothing starts the queued job before boot")
    runner("_power-boot")
    check(wait_state_any(rj, b, {"done"})["state"] == "done", "boot unit starts jobs queued before the stop")

    # nebius provider: CLI calls and their arguments, with a stub `nebius`
    stub = TMP / "nebius-stub"
    log = TMP / "nebius-argv.log"
    stub.write_text(f'#!/bin/bash\necho "$*" >> {log}\n[[ "$3" == get ]] && echo \'"running"\'\nexit 0\n')
    stub.chmod(0o755)
    sys.path.insert(0, str(REPO / "host/power"))
    from providers import ProviderError, nebius
    ncfg = {"cli": str(stub), "instance_id": "computeinstance-e00abc", "profile": "rjob-autostop"}
    check(nebius.state(ncfg) == "RUNNING", "nebius provider reads .status.state, unquoted and upper-cased")
    nebius.stop(ncfg)
    nebius.start(ncfg)
    lines = log.read_text().splitlines()
    check(lines[0].startswith("compute instance get --format jsonpath={.status.state} --id computeinstance-e00abc")
          and all("--profile rjob-autostop" in l for l in lines), f"nebius get/profile arguments {lines[0]}")
    check(lines[1].startswith("compute instance stop --async") and lines[2].startswith("compute instance start --async"),
          "nebius stop and start are asynchronous")
    for badcfg in ({**ncfg, "instance_id": "x; rm -rf /"}, {**ncfg, "profile": "--debug"}, {**ncfg, "cli": "/nonexistent"}):
        try:
            nebius.state(badcfg)
            ok = False
        except ProviderError:
            ok = True
        check(ok, f"nebius provider rejects {sorted(set(badcfg.items()) - set(ncfg.items()))}")

    env = dict(os.environ, HOME=str(home))
    bad = [c for c in ("_power-check 1 1", "_power-abort", "_power-boot", "hold", "request-stop now")
           if subprocess.run([str(home / "rjob/bin/run-job")], env=dict(env, SSH_ORIGINAL_COMMAND=c),
                             capture_output=True, text=True).returncode != 2]
    check(not bad, f"internal power verbs and bad arguments rejected over ssh {bad}")


def nebius_setup_scripts():
    """host/power/nebius-grant-power.sh (laptop) and nebius-setup-profile.sh (VM) against a stub CLI."""
    import getpass
    d = TMP / "nb"
    (d / "bin").mkdir(parents=True)
    st = d / "state"
    st.mkdir()
    log = d / "argv.log"
    stub = d / "bin/nebius"
    stub.write_text(f"""#!/bin/bash
echo "$*" >> {log}
S={st}
arg() {{ local k="$1"; shift; while [ $# -gt 0 ]; do [ "$1" = "$k" ] && {{ echo "$2"; return; }}; shift; done; }}
once() {{ if [ -e "$S/$1" ]; then echo "ALREADY_EXISTS: $1" >&2; exit 1; fi; touch "$S/$1"; }}
case "$1 $2 $3" in
  "compute instance get-by-name") echo computeinstance-e00test ;;
  "compute instance get") case "$*" in *metadata.name*) echo '"testvm"' ;; *) echo '"'"${{STUB_STATE:-RUNNING}}"'"' ;; esac ;;
  "iam service-account get-by-name") [ -e "$S/sa" ] && echo serviceaccount-e00sa || exit 1 ;;
  "iam service-account create") touch "$S/sa"; echo serviceaccount-e00sa ;;
  "iam service-account list") exit "${{STUB_IAM_RC:-1}}" ;;
  "iam group get-by-name") [ -e "$S/group" ] && echo group-e00grp || exit 1 ;;
  "iam group create") touch "$S/group"; echo group-e00grp ;;
  "iam group-membership create") once member ;;
  "iam access-permit create") once permit ;;
  "iam auth-public-key generate") echo '{{"subject-credentials": "secret"}}' > "$(arg --output "$@")" ;;
  *) case "$1 $2" in
       "profile list") cat "$S/profiles" 2>/dev/null ;;
       "profile create") arg --profile "$@" >> "$S/profiles" ;;
       *) echo "stub: unexpected $*" >&2; exit 3 ;;
     esac ;;
esac
""")
    stub.chmod(0o755)
    env = dict(os.environ, PATH=f"{d / 'bin'}:{os.environ['PATH']}")
    out = d / "out"
    out.mkdir()
    grant = ["bash", str(REPO / "host/power/nebius-grant-power.sh"), "--tenant-id", "tenant-e00t",
             "--project-id", "project-e00p", "--instance-name", "TestVM_1", "--out", str(out)]

    r = sh(*grant, "--expires-at", "2027-10-22T00:00:00Z", env=env, check_rc=False)
    lines = log.read_text().splitlines()
    cred = out / "rjob-autostop-testvm-1.json"
    check(r.returncode == 0 and cred.exists() and oct(cred.stat().st_mode & 0o777) == "0o600",
          f"grant script writes a mode-600 credentials file {r.stderr[-200:]}")
    check(any(l.startswith("iam access-permit create --parent-id group-e00grp --resource-id computeinstance-e00test "
                           "--role compute.instance-power-operator") for l in lines),
          "permit: power-operator, granted to the group, on the instance (not the project)")
    check(any("iam service-account create --parent-id project-e00p --name rjob-autostop-testvm-1" in l for l in lines)
          and any("iam group create --parent-id tenant-e00t --name rjob-power-testvm-1" in l for l in lines),
          "account in the project, group in the tenant, names from the instance")
    check(any("--expires-at 2027-10-22T00:00:00Z" in l for l in lines), "key expiry passed through")
    check("nebius-setup-profile.sh" in r.stdout and "--instance-id computeinstance-e00test" in r.stdout,
          "grant script prints the VM step with the ids filled in")
    r = sh(*grant, env=env, check_rc=False)
    check(r.returncode != 0 and "exists" in r.stderr, "grant script never overwrites a credentials file")
    cred.rename(d / "moved.json")
    n_create = sum("service-account create" in l for l in log.read_text().splitlines())
    r = sh(*grant, env=env, check_rc=False)
    check(r.returncode == 0 and n_create == sum("service-account create" in l for l in log.read_text().splitlines())
          and r.stdout.count("already exists") == 2, "re-run reuses account, group, membership and permit")
    r = sh("bash", str(REPO / "host/power/nebius-grant-power.sh"), "--tenant-id", "-x", "--project-id", "p",
           "--instance-id", "i", env=env, check_rc=False)
    check(r.returncode != 0 and "bad --tenant-id" in r.stderr, "grant script rejects ids that look like flags")

    etc = d / "etc"
    src = d / "tmp-cred.json"
    src.write_text('{"subject-credentials": "secret"}')
    setup = ["bash", str(REPO / "host/power/nebius-setup-profile.sh"), "--credentials", str(src),
             "--project-id", "project-e00p", "--instance-id", "computeinstance-e00test",
             "--worker", getpass.getuser(), "--cli", str(stub)]
    senv = dict(env, RJOB_POWER_ETC=str(etc))
    r = sh(*setup, env=senv, check_rc=False)
    dest = etc / "tmp-cred.json"
    cfgtext = (etc / "autostop.toml").read_text() if (etc / "autostop.toml").exists() else ""
    check(r.returncode == 0 and dest.exists() and not src.exists() and oct(dest.stat().st_mode & 0o777) == "0o600"
          and oct(etc.stat().st_mode & 0o777) == "0o700", f"setup script moves the key into a 700 dir, mode 600 {r.stderr[-300:]}")
    check('instance_id = "computeinstance-e00test"' in cfgtext and 'profile = "rjob-autostop"' in cfgtext
          and f'cli = "{stub}"' in cfgtext and f'worker = "{getpass.getuser()}"' in cfgtext and "REPLACE-ME" not in cfgtext,
          "setup script fills in autostop.toml")
    check(any(l.startswith("profile create --endpoint api.nebius.cloud --service-account-file " + str(dest))
              for l in log.read_text().splitlines()), "profile authenticates with the moved key")
    src.write_text("{}")
    r = sh(*setup, env=senv, check_rc=False)
    check(r.returncode == 0 and "exists; left as is" in r.stdout and "already filled in; kept" in r.stdout,
          "setup re-run keeps the profile and a filled-in config")
    src.write_text("{}")
    r = sh(*setup, env=dict(senv, STUB_IAM_RC="0"), check_rc=False)
    check(r.returncode != 0 and "broader" in r.stderr, "setup refuses a credential that can do more than power operations")
    src.write_text("{}")
    r = sh(*setup, env=dict(senv, STUB_STATE="STOPPED"), check_rc=False)
    check(r.returncode != 0 and "expected RUNNING" in r.stderr, "setup refuses when the VM state is not RUNNING")


def wait_state_any(rj, job_id, states, timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = json.loads(rj("status", job_id).stdout)
        if st["state"] in states:
            return st
        time.sleep(0.3)
    return st


def main():
    setup_host()
    main_repo, sub = setup_repos()
    runs = TMP / "runs"

    # --- code job: push repo + submodule, build, overlay config, fetch
    spec = write_spec(TMP / "jobs/code", f'''
name = "code-job"
runs_dir = "{runs}"
[code]
path = "{main_repo}"
build_steps = [["python3", "-c", "open('built.txt','w').write('built')"]]
[resources]
device = "cpu"
cpus = 2
[run]
argv = ["python", "main.py"]
outputs = "output"
files = ["configs/c.json"]
''', {"configs/c.json": '{"msg": "overlay-config"}'})
    dry = json.loads(rjob("submit", str(spec), "--dry-run").stdout)
    check(dry["code"]["submodules"]["ext/sub"]["repo"] == "subrepo", "dry-run resolves submodule repo name")
    jid = rjob("submit", str(spec)).stdout.strip()
    st = json.loads(rjob("wait", jid, "--interval", "0.3").stdout)
    check(st["state"] == "done", f"code job done ({st['state']})")
    out = rjob("fetch", jid).stdout
    res = (runs / jid / "outputs/result.txt").read_text()
    check(res == "overlay-config lib-v1 built 2", f"result uses overlay, submodule, build, cpus ({res!r})")
    check("3 fetched" in out, "first fetch downloads 3 files")
    check("0 fetched, 3 already present" in rjob("fetch", jid).stdout, "second fetch is a no-op (resume)")
    (runs / jid / "outputs/sub/big.bin").write_bytes(b"corrupt")
    check("1 fetched" in rjob("fetch", jid).stdout, "corrupted file is re-fetched")
    inc = rjob("fetch", jid, "--dest", str(TMP / "inc"), "--include", "*.json").stdout
    check("1 fetched" in inc and not (TMP / "inc/outputs/result.txt").exists(), "--include fetches subset")
    prov = json.loads((runs / jid / "provenance.json").read_text())
    check(prov["code"]["sha"] == git(main_repo, "rev-parse", "HEAD"), "provenance has repo sha")
    check(prov["code"]["submodules"]["ext/sub"]["sha"] == git(sub, "rev-parse", "HEAD"), "provenance has submodule sha")
    check(len(prov["inputs"]["configs/c.json"]) == 64, "provenance hashes overlaid inputs")
    check(not (main_repo / "output").exists(), "local repo untouched")
    srcs = list((HOSTHOME / "rjob/builds").glob("mainrepo@*/src"))
    check(len(srcs) == 1 and not (srcs[0] / "output").exists()
          and json.loads((srcs[0] / "configs/c.json").read_text())["msg"] == "repo-config",
          "shared build tree not written by the job")
    check(sh("git", "-C", str(main_repo), "for-each-ref", "refs/rjob").stdout == "", "temporary push refs removed")

    # --- build reuse
    jid2 = rjob("submit", str(spec)).stdout.strip()
    rjob("wait", jid2, "--interval", "0.3")
    check("reusing build" in rjob("logs", jid2).stdout, "second job reuses the build")

    # --- dirty tree refused
    (main_repo / "main.py").write_text("# dirty\n")
    r = rjob("submit", str(spec), check_rc=False)
    check(r.returncode != 0 and "uncommitted" in r.stderr, "dirty tree refused")
    git(main_repo, "checkout", "main.py")

    # --- submodule override -> new build, new sha
    (sub / "lib.txt").write_text("lib-v2\n")
    git(sub, "commit", "-qam", "sub v2")
    v2 = git(sub, "rev-parse", "HEAD")
    git(main_repo / "ext/sub", "fetch", "-q", "origin")
    spec_ov = Path(str(spec).replace("code/job.toml", "code/job_ov.toml"))
    spec_ov.write_text(spec.read_text() + f'\n[code.submodules."ext/sub"]\nsha = "{v2}"\n')
    jid3 = rjob("submit", str(spec_ov)).stdout.strip()
    rjob("wait", jid3, "--interval", "0.3", check_rc=False)
    rjob("fetch", jid3)
    check((runs / jid3 / "outputs/result.txt").read_text().split()[1] == "lib-v2", "submodule override builds v2")
    check(len(list((HOSTHOME / "rjob/builds").glob("mainrepo@*"))) == 2, "override gets its own build")

    # --- script jobs: queue, cancel, timeout
    sleeper = "import time, os\nos.makedirs('outputs', exist_ok=True)\ntime.sleep(float(open('t').read()))\n"
    ids = []
    for i in range(3):
        s = write_spec(TMP / f"jobs/s{i}", f'''
name = "sleep{i}"
runs_dir = "{runs}"
[resources]
cpus = 1
[run]
argv = ["python", "main.py"]
files = ["main.py", "t"]
''', {"main.py": sleeper, "t": "30"})
        ids.append(rjob("submit", str(s)).stdout.strip())
    time.sleep(1.5)
    states = [json.loads(rjob("status", i).stdout)["state"] for i in ids]
    check(states[:2] == ["running", "running"] and states[2] == "queued", f"cpu slots respected {states}")
    check("cancelled" in rjob("cancel", ids[0]).stdout, "cancel running job")
    st = wait_state(ids[2], {"running"}, 15)
    check(st["state"] == "running", "queued job starts after cancel frees a slot")
    pg = json.loads(rjob("status", ids[0]).stdout).get("child_pgid")
    alive = subprocess.run(["kill", "-0", str(pg)], capture_output=True).returncode == 0
    check(not alive, "cancelled job's process is gone")
    for i in ids[1:]:
        rjob("cancel", i)

    s = write_spec(TMP / "jobs/to", f'''
name = "timeout"
runs_dir = "{runs}"
[resources]
timeout_hours = 0.0005
[run]
argv = ["python", "main.py"]
files = ["main.py", "t"]
''', {"main.py": sleeper, "t": "60"})
    jt = rjob("submit", str(s)).stdout.strip()
    st = wait_state(jt, {"timeout", "failed", "done"}, 30)
    check(st["state"] == "timeout", f"timeout enforced ({st['state']})")

    # --- data upload
    big = TMP / "input.bin"
    big.write_bytes(os.urandom(200000))
    ref = rjob("put-data", str(big)).stdout.strip()
    check(ref.startswith("sha256:") and rjob("put-data", str(big)).stdout.strip() == ref, "put-data idempotent")
    s = write_spec(TMP / "jobs/data", f'''
name = "data"
runs_dir = "{runs}"
[data]
"inputs/x.bin" = "{big}"
[run]
argv = ["python", "main.py"]
files = ["main.py"]
''', {"main.py": "import os,hashlib\nos.makedirs('outputs', exist_ok=True)\nopen('outputs/h','w').write(hashlib.sha256(open('inputs/x.bin','rb').read()).hexdigest())\n"})
    jd = rjob("submit", str(s)).stdout.strip()
    rjob("wait", jd, "--interval", "0.3", check_rc=False)
    rjob("fetch", jd)
    check("sha256:" + (runs / jd / "outputs/h").read_text() == ref, "data input visible to job")

    # --- hostile commands over the locked channel
    hostile = ["", "ls", "bash -c id", "status ../../etc", "status 1;id", f"logs {jid} 10;id",
               "_supervise " + jid, "_tick", "push ../x", "push a/b", "has x notasha",
               "cleanup job ..", "fetch $(id)", "logs 20990101-000000-abcdef", "status " + jid + " extra", "status 'x"]
    bad = [c for c in hostile if host(c).returncode != 2]
    check(not bad, f"hostile commands rejected {bad}")
    p = subprocess.run([str(HOSTHOME / "rjob/bin/run-job")], input=b"../job.json\n", capture_output=True,
                       env=dict(os.environ, SSH_ORIGINAL_COMMAND=f"fetch {jid}", HOME=str(HOSTHOME)))
    check(p.returncode == 2, "fetch path traversal rejected")
    git(main_repo, "update-ref", "refs/heads/evil", "HEAD")
    bundle = subprocess.run(["git", "-C", str(main_repo), "bundle", "create", "-", "refs/heads/evil"],
                            capture_output=True).stdout
    git(main_repo, "update-ref", "-d", "refs/heads/evil")
    p = subprocess.run([str(HOSTHOME / "rjob/bin/run-job")], input=bundle, capture_output=True,
                       env=dict(os.environ, SSH_ORIGINAL_COMMAND="push mainrepo", HOME=str(HOSTHOME)))
    check(p.returncode == 2, "bundle with non-rjob refs rejected")

    info = json.loads(rjob("info").stdout)
    check(info["slots"]["cpu"] == 2 and "builds" in info, "info reports slots and builds")
    print(rjob("list").stdout)
    multi_host()
    autostop()
    nebius_setup_scripts()
    print(f"\n{len(FAILS)} failures" if FAILS else "\nall checks passed")
    if not FAILS:
        shutil.rmtree(TMP, ignore_errors=True)
    else:
        print("scratch kept at", TMP)
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
