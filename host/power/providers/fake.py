"""Fake power provider for tests: the instance state lives in a JSON file.

Config ([fake]): state_file = "/path/state.json"; fail = true makes every call raise;
down_marker = "/path" is created by stop and removed by start (the tests' fake ssh
treats a host with that file as unreachable); on_start = [argv] runs after a start
(the tests' stand-in for rjob-boot.service). start/stop are instantaneous.
The file holds {"state": "RUNNING", "calls": ["stop", ...]}.
"""
# Created by: Alex Myasnikov
# Date: October 22, 2026
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from . import ProviderError


def _load(cfg: dict) -> tuple[Path, dict]:
    if cfg.get("fail"):
        raise ProviderError("fake provider failure")
    path = Path(cfg["state_file"])
    return path, (json.loads(path.read_text()) if path.exists() else {"state": "RUNNING", "calls": []})


def _set(cfg: dict, call: str, new_state: str) -> None:
    path, st = _load(cfg)
    st["calls"].append(call)
    st["state"] = new_state
    path.write_text(json.dumps(st))


def state(cfg: dict) -> str:
    return _load(cfg)[1]["state"]


def start(cfg: dict) -> None:
    _set(cfg, "start", "RUNNING")
    if cfg.get("down_marker"):
        Path(cfg["down_marker"]).unlink(missing_ok=True)
    if cfg.get("on_start"):
        subprocess.run(cfg["on_start"], check=True, capture_output=True)


def stop(cfg: dict) -> None:
    _set(cfg, "stop", "STOPPED")
    if cfg.get("down_marker"):
        Path(cfg["down_marker"]).touch()
