#!/bin/bash
# install.sh — set up the remote-jobs host side for the CURRENT user.
# Run as the unprivileged worker account (on the Mac: `sudo -iu gpuworker`, then
# `bash /path/to/remote-jobs/host/install.sh`). Safe to re-run (updates run_job.py).
#
# Creates:  ~/rjob/bin/{run-job,run_job.py,python3}   forced-command shim + runner
#           ~/rjob/venv                                default Python with torch (script jobs, `info`)
#           ~/rjob/config.toml                         slots and tool paths (kept if present)
# Installs: uv (into ~/.local/bin) if missing.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
R="$HOME/rjob"
mkdir -p "$R/bin" "$R/mirrors" "$R/builds" "$R/data" "$R/jobs" "$R/locks"

if ! command -v uv >/dev/null 2>&1 && [[ ! -x "$HOME/.local/bin/uv" ]]; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh
fi
UV="$(command -v uv || echo "$HOME/.local/bin/uv")"

"$UV" python install 3.12
ln -sf "$("$UV" python find 3.12)" "$R/bin/python3"
install -m 755 "$HERE/run_job.py" "$R/bin/run_job.py"
install -m 755 "$HERE/run-job" "$R/bin/run-job"

if [[ ! -x "$R/venv/bin/python" ]]; then
  "$UV" venv --python 3.12 "$R/venv"
  VIRTUAL_ENV="$R/venv" "$UV" pip install torch
fi

if [[ ! -f "$R/config.toml" ]]; then
  ncpu=$( (sysctl -n hw.ncpu 2>/dev/null || nproc) | head -1)
  cat > "$R/config.toml" <<CFG
# remote-jobs host config (read by run_job.py)
gpu_slots = 1                 # concurrent jobs with device = mps|cuda|auto
cpu_slots = $((ncpu - 1))     # sum of resources.cpus over running jobs
uv = "$UV"
python = "$R/venv/bin/python" # used for jobs without [code] and for \`info\`
keep_awake = true             # wrap jobs in caffeinate -i on macOS
CFG
fi

echo "== checks"
git --version
if command -v clang++ >/dev/null 2>&1; then
  printf '#include <format>\nint main(){return std::format("{}",1).size()!=1;}\n' > "$R/locks/fmt.cpp"
  if clang++ -std=c++20 "$R/locks/fmt.cpp" -o "$R/locks/fmt" 2>/dev/null && "$R/locks/fmt"; then
    echo "clang++ C++20 <format>: ok"
  else
    echo "WARNING: clang++ lacks C++20 <format> (tcgraph needs it): update Xcode CLT or install LLVM" >&2
  fi
  rm -f "$R/locks/fmt.cpp" "$R/locks/fmt"
else
  echo "WARNING: no clang++ (Mac: xcode-select --install, as an admin user)" >&2
fi
"$R/venv/bin/python" -c "import torch; print('torch', torch.__version__, 'mps', torch.backends.mps.is_available(), 'cuda', torch.cuda.is_available())" || echo "WARNING: torch missing in ~/rjob/venv" >&2
SSH_ORIGINAL_COMMAND=info "$R/bin/run-job" | head -5
echo "== done. authorized_keys line: restrict,command=\"$R/bin/run-job\" <VM public key>"
