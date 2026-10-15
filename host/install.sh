#!/bin/bash
# install.sh — set up the remote-jobs host side for the CURRENT user.
# Run as the unprivileged worker account (on the Mac: `sudo -iu gpuworker`, then
# `bash /path/to/remote-jobs/host/install.sh`). Safe to re-run (updates run_job.py).
# Works on macOS (MPS) and Linux (CUDA via nvidia-smi, or CPU only).
#
# RJOB_ROOT=/scratch/gpuworker/rjob bash install.sh   puts the state on another disk
# (e.g. local scratch instead of an NFS home); ~/rjob becomes a symlink to it.
#
# Creates:  ~/rjob/bin/{run-job,run_job.py,python3}   forced-command shim + runner
#           ~/rjob/venv                                default Python with torch (script jobs, `info`)
#           ~/rjob/config.toml                         slots and tool paths (kept if present)
# Installs: uv (into ~/.local/bin) if missing.
#
# Created by: Alex Myasnikov
# Date: October 5, 2026
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
R="$HOME/rjob"
if [[ -n "${RJOB_ROOT:-}" && "$RJOB_ROOT" != "$R" ]]; then
  mkdir -p "$RJOB_ROOT"
  if [[ ! -e "$R" ]]; then
    ln -s "$RJOB_ROOT" "$R"
  elif [[ "$(cd "$R" && pwd -P)" != "$(cd "$RJOB_ROOT" && pwd -P)" ]]; then
    echo "ERROR: $R exists and is not $RJOB_ROOT; move it first" >&2
    exit 1
  fi
fi
mkdir -p "$R/bin" "$R/mirrors" "$R/builds" "$R/data" "$R/jobs" "$R/locks"

# Always the worker's own uv: never one from another user's home found on PATH.
UV="$HOME/.local/bin/uv"
if [[ ! -x "$UV" ]]; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh
fi

"$UV" python install 3.12
ln -sf "$("$UV" python find 3.12)" "$R/bin/python3"
install -m 755 "$HERE/run_job.py" "$R/bin/run_job.py"
install -m 755 "$HERE/run-job" "$R/bin/run-job"

if [[ ! -x "$R/venv/bin/python" ]]; then
  "$UV" venv --python 3.12 "$R/venv"
  VIRTUAL_ENV="$R/venv" "$UV" pip install torch
fi

if [[ ! -f "$R/config.toml" ]]; then
  if [[ "$(uname -s)" == Darwin ]]; then
    ncpu=$(sysctl -n hw.ncpu)
    mem_kb=$(( $(sysctl -n hw.memsize) / 1024 ))
    gpu_ids="[0]"             # the Apple GPU (MPS) is one device
  else
    ncpu=$(nproc)
    mem_kb=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)
    ngpu=0
    if command -v nvidia-smi >/dev/null 2>&1; then
      ngpu=$(nvidia-smi -L 2>/dev/null | grep -c '^GPU ' || true)
    fi
    gpu_ids="[$(seq -s ', ' 0 $((ngpu - 1)) 2>/dev/null)]"
  fi
  mem_gb=$(( mem_kb * 85 / 100 / 1048576 ))   # 15% headroom for the OS and the runner
  cat > "$R/config.toml" <<CFG
# remote-jobs host config (read by run_job.py)
gpu_ids = $gpu_ids            # GPUs jobs may use; one job per id (CUDA_VISIBLE_DEVICES on Linux)
cpu_slots = $((ncpu - 1))     # sum of resources.cpus over running jobs
mem_gb = $mem_gb              # sum of resources.mem_gb over running jobs; 0 = unlimited
uv = "$UV"
python = "$R/venv/bin/python" # used for jobs without [code] and for \`info\`
keep_awake = true             # wrap jobs in caffeinate -i on macOS (ignored elsewhere)
# devices = ["cpu", "cuda"]   # default: detected (mps on Apple silicon, cuda if nvidia-smi exists)
CFG
else
  for key in gpu_ids mem_gb; do
    grep -q "^$key" "$R/config.toml" || echo "NOTE: $R/config.toml has no $key (see run_job.py DEFAULT_CONFIG); defaults apply"
  done
fi

echo "== checks"
git --version
cxx=""
for c in c++ clang++ g++; do command -v "$c" >/dev/null 2>&1 && { cxx=$c; break; }; done
if [[ -n "$cxx" ]]; then
  printf '#include <format>\nint main(){return std::format("{}",1).size()!=1;}\n' > "$R/locks/fmt.cpp"
  if "$cxx" -std=c++20 "$R/locks/fmt.cpp" -o "$R/locks/fmt" 2>/dev/null && "$R/locks/fmt"; then
    echo "$cxx C++20 <format>: ok"
  else
    echo "WARNING: $cxx lacks C++20 <format> (tcgraph needs it): Mac: update Xcode CLT; Linux: gcc >= 13 or clang >= 17" >&2
  fi
  rm -f "$R/locks/fmt.cpp" "$R/locks/fmt"
else
  echo "WARNING: no C++ compiler (Mac: xcode-select --install; Linux: an admin installs gcc >= 13 and cmake)" >&2
fi
command -v cmake >/dev/null 2>&1 || echo "WARNING: no cmake (C++ extensions such as tcgraph need it)" >&2
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi -L
fi
"$R/venv/bin/python" -c "import torch; print('torch', torch.__version__, 'mps', torch.backends.mps.is_available(), 'cuda', torch.cuda.is_available())" || echo "WARNING: torch missing in ~/rjob/venv" >&2
SSH_ORIGINAL_COMMAND=info "$R/bin/run-job" | head -5
echo "== done. authorized_keys line: restrict,command=\"$R/bin/run-job\" <VM public key>"
