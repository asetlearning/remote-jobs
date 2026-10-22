"""Nebius AI Cloud power provider: wraps `nebius compute instance get|start|stop`.

Config ([nebius] in autostop.toml):
    instance_id = "computeinstance-..."   the VM to control (this host, for autostop)
    profile = "rjob-autostop"             CLI profile of a service account whose group holds
                                          compute.instance-power-operator on this instance only
    cli = "/root/.nebius/bin/nebius"      optional; default: `nebius` on PATH
    timeout_s = 120                       optional
"""
# Created by: Alex Myasnikov
# Date: October 22, 2026
from __future__ import annotations

import re
import subprocess

from . import ProviderError

RE_INSTANCE = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")
RE_PROFILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")  # no leading dash: never read as a flag


def _cli(cfg: dict, *args: str) -> str:
    iid, profile = cfg.get("instance_id", ""), cfg.get("profile", "")
    if not RE_INSTANCE.match(iid):
        raise ProviderError(f"bad or missing nebius.instance_id {iid!r}")
    cmd = [cfg.get("cli", "nebius"), "compute", "instance", *args, "--id", iid, "--no-check-update", "--no-progress"]
    if profile:
        if not RE_PROFILE.match(profile):
            raise ProviderError(f"bad nebius.profile {profile!r}")
        cmd += ["--profile", profile]
    try:
        p = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           timeout=float(cfg.get("timeout_s", 120)))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProviderError(f"nebius {args[0]}: {exc}") from exc
    if p.returncode != 0:
        raise ProviderError(f"nebius {args[0]} failed ({p.returncode}): {p.stderr.strip()[-400:]}")
    return p.stdout


def state(cfg: dict) -> str:
    out = _cli(cfg, "get", "--format", "jsonpath={.status.state}").strip().strip("\"'").upper()
    return out or "UNKNOWN"


def start(cfg: dict) -> None:
    _cli(cfg, "start", "--async")


def stop(cfg: dict) -> None:
    _cli(cfg, "stop", "--async")  # without --async the CLI waits, and this VM goes down under it
