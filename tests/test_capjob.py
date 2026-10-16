#!/usr/bin/env python3
"""End-to-end checks for client/capjob: the cap, exit codes and the rjob fallback.

Needs a systemd user session (systemd-run --user); skips otherwise. rjob is replaced by
a stub on PATH, so no host is contacted.
"""
# Created by: Alex Myasnikov
# Date: October 16, 2026
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

CAPJOB = Path(__file__).resolve().parent.parent / "client" / "capjob"
HOG = "x = bytearray(300 * 2**20)"
FAILS = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global FAILS
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + (f"  ({detail})" if detail and not ok else ""))
    FAILS += not ok


def capjob(args: list[str], cwd: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(CAPJOB), *args], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=120)


def git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd,
                   check=True, capture_output=True)


def main() -> int:
    probe = subprocess.run(["systemd-run", "--user", "--scope", "--quiet", "true"],
                           capture_output=True)
    if probe.returncode != 0 or not shutil.which("choom"):
        print("skip: no systemd user session or no choom")
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        stub_bin = tmp / "bin"
        stub_bin.mkdir()
        (stub_bin / "rjob").write_text(
            "#!/bin/sh\necho \"$@\" >> \"$RJOB_STUB_LOG\"\necho 'host: stub' >&2\necho 20990101-000000-stub\n")
        (stub_bin / "rjob").chmod(0o755)
        project = tmp / "proj"
        project.mkdir()
        (project / "rjob.toml").write_text('[[hosts]]\nalias = "stub"\n')
        log = tmp / "rjob.log"
        env = {**os.environ, "PATH": f"{stub_bin}:{os.environ['PATH']}",
               "RJOB_PROJECT": str(project), "RJOB_STUB_LOG": str(log)}
        env.pop("OMP_NUM_THREADS", None)

        # 1. a normal run passes its exit code through; cap env and oom_score_adj applied
        r = capjob(["--mem", "200M", "--cpus", "3", "--", sys.executable, "-c",
                    "import os,sys; print(os.environ['OMP_NUM_THREADS'],"
                    " open('/proc/self/oom_score_adj').read().strip()); sys.exit(3)"],
                   tmp, env)
        check("normal run: exit code passed through", r.returncode == 3, r.stderr)
        check("normal run: threads = --cpus, oom_score_adj = 1000",
              r.stdout.split() == ["3", "1000"], r.stdout)

        # 2. killed at the cap with --no-fallback: exit 75, nothing submitted
        r = capjob(["--mem", "100M", "--swap", "0", "--no-fallback", "--",
                    sys.executable, "-c", HOG], tmp, env)
        check("cap kill, --no-fallback: exit 75", r.returncode == 75, r.stderr)
        check("cap kill, --no-fallback: rjob not called", not log.exists())

        # 3. killed outside a git repo, no --out: draft written, exit 75
        r = capjob(["--mem", "100M", "--swap", "0", "--", sys.executable, "-c", HOG], tmp, env)
        drafts = list((project / "jobs" / "capjob").glob("*/job.toml"))
        check("cap kill, unsafe: exit 75", r.returncode == 75, r.stderr)
        check("cap kill, unsafe: one draft job.toml", len(drafts) == 1)
        check("cap kill, unsafe: blockers recorded in the draft",
              bool(drafts) and "NOT auto-submitted" in drafts[0].read_text())
        check("cap kill, unsafe: rjob not called", not log.exists())

        # 4. killed in a clean repo with --out: generated spec auto-submitted, exit 76
        repo = tmp / "repo"
        (repo / "sub").mkdir(parents=True)
        (repo / "sub" / "hog.py").write_text(HOG + "\n")
        git("init", "-q", cwd=repo)
        git("add", ".", cwd=repo)
        git("commit", "-qm", "t", cwd=repo)
        r = capjob(["--mem", "100M", "--swap", "0", "--out", "out", "--name", "hog", "--",
                    "python3", "hog.py"], repo / "sub", env)
        check("cap kill, safe: exit 76", r.returncode == 76, r.stderr)
        check("cap kill, safe: job id printed", r.stdout.strip() == "20990101-000000-stub", r.stdout)
        spec = next((project / "jobs" / "capjob").glob("hog-*/job.toml"), None)
        text = spec.read_text() if spec else ""
        check("cap kill, safe: spec has repo, cd into sub, outputs sub/out",
              f'path = "{repo}"' in text and "cd sub && exec python3 hog.py" in text
              and 'outputs = "sub/out"' in text, text)
        check("cap kill, safe: rjob submit called with the spec",
              log.exists() and log.read_text().strip() == f"submit {spec}")

        # 5. a dirty tree blocks auto-submit
        (repo / "sub" / "hog.py").write_text(HOG + "  # edited\n")
        log.unlink(missing_ok=True)
        r = capjob(["--mem", "100M", "--swap", "0", "--out", "out", "--", "python3", "hog.py"],
                   repo / "sub", env)
        check("cap kill, dirty tree: exit 75, not submitted",
              r.returncode == 75 and not log.exists(), r.stderr)

        # 6. --rjob JOB.toml is submitted as given
        given = tmp / "given.toml"
        given.write_text('name = "x"\n')
        r = capjob(["--mem", "100M", "--swap", "0", "--rjob", str(given), "--",
                    sys.executable, "-c", HOG], tmp, env)
        check("cap kill, --rjob: exit 76 and the given spec submitted",
              r.returncode == 76 and log.read_text().strip() == f"submit {given}", r.stderr)

    print("\nall checks passed" if not FAILS else f"\n{FAILS} check(s) failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
