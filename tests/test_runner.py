#!/usr/bin/env python3
"""End-to-end test of client/rjob + host/run_job.py without a real SSH host.

A fake `ssh` runs the forced-command shim exactly as sshd would: the client's
command goes into $SSH_ORIGINAL_COMMAND and HOME points at a scratch host home.
Covers push (incl. submodule), build reuse, submit, queue/slots, wait, fetch
(+ resume, --include), provenance, dirty-tree refusal, cancel, timeout, data
upload and hostile commands.  Run: python3 tests/test_runner.py
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
HOSTHOME = TMP / "hosthome"
RJOB = [sys.executable, str(REPO / "client/rjob"), "--host", "testhost"]
FAILS = []


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def sh(*cmd, cwd=None, check_rc=True, env=None, inp=None):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env, input=inp)
    if check_rc and p.returncode != 0:
        raise RuntimeError(f"{cmd}: {p.returncode}\n{p.stdout}\n{p.stderr}")
    return p


def setup_host():
    b = HOSTHOME / "rjob/bin"
    b.mkdir(parents=True)
    shutil.copy(REPO / "host/run_job.py", b)
    shutil.copy(REPO / "host/run-job", b)
    (b / "python3").symlink_to(sys.executable)
    (HOSTHOME / "rjob/config.toml").write_text(
        'gpu_slots = 1\ncpu_slots = 2\nkeep_awake = false\nuv = "/nonexistent/uv"\n')
    fake = TMP / "fakessh"
    fake.write_text(f'#!/bin/bash\n# fake ssh: $1 = host, $2 = command string\n'
                    f'SSH_ORIGINAL_COMMAND="$2" HOME="{HOSTHOME}" exec "{HOSTHOME}/rjob/bin/run-job"\n')
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
    print(f"\n{len(FAILS)} failures" if FAILS else "\nall checks passed")
    if not FAILS:
        shutil.rmtree(TMP, ignore_errors=True)
    else:
        print("scratch kept at", TMP)
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
