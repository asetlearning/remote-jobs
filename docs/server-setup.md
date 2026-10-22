# Setup: Linux server host

This adds a Linux server (x86_64, NVIDIA GPUs or CPU only) as a remote-jobs host. The model is the same as on the Mac (see [mac-setup.md](mac-setup.md)): a dedicated worker account, an SSH key locked to the `run-job` forced command, and native per-SHA builds. Steps marked (admin) need a sudo-capable account on the server.

## Server

1. **Worker account (admin).** It must hold nothing of value: no keys, no credentials, no personal files.
   ```bash
   sudo useradd -m -s /bin/bash gpuworker
   sudo passwd -l gpuworker          # no password login; the locked key is the only way in
   ```
   On NVIDIA hosts the account must be able to open `/dev/nvidia*`. On most distributions that is the default; otherwise add it to the `video` (or `render`) group.
2. **Build tools (admin).** `git`, `curl`, a C++20 compiler with `<format>` (gcc ≥ 13 or clang ≥ 17), `cmake`, and the NVIDIA driver (`nvidia-smi` works). CUDA toolkits are not needed: PyTorch wheels bring their own CUDA runtime.
   ```bash
   sudo apt install -y git curl build-essential g++-13 cmake    # Debian/Ubuntu example
   ```
3. **Install the runner (as gpuworker).** Copy or clone this repo where gpuworker can read it, then:
   ```bash
   sudo -iu gpuworker
   bash /path/to/remote-jobs/host/install.sh
   # or, to keep builds and jobs on a local scratch disk instead of the home dir:
   RJOB_ROOT=/scratch/gpuworker/rjob bash /path/to/remote-jobs/host/install.sh
   ```
   - It installs uv, Python 3.12, `~/rjob/venv` with torch (CUDA wheels on x86_64), `~/rjob/bin/{run-job,run_job.py}` and `~/rjob/config.toml`.
   - The config gets `gpu_ids` from `nvidia-smi -L`, `cpu_slots` = cores − 1 and `mem_gb` = 85% of RAM. Preview it first with `bash install.sh --print-config`, and override any value for a new config with `RJOB_GPU_IDS="0,1"` (`""` = no GPU), `RJOB_CPU_SLOTS=N` or `RJOB_MEM_GB=N` (0 = unlimited).
   - Other Unix systems than Linux and macOS get CPU slots only (no memory budget, no GPUs) and a warning; use the overrides, or set `devices = [...]` in the config.
   - It checks the compiler, cmake and `nvidia-smi`, and prints `info`; `default_python.cuda` must be `true` on a GPU host.
   - Re-run it after `host/run_job.py` changes.
4. **Authorized key (as gpuworker).** Use the VM public key from step 6. One line:
   ```bash
   mkdir -p ~/.ssh && chmod 700 ~/.ssh
   echo 'restrict,command="/home/gpuworker/rjob/bin/run-job",from="<VM egress IP>" ssh-ed25519 AAAA... vm-agents->gpuserver' >> ~/.ssh/authorized_keys
   chmod 600 ~/.ssh/authorized_keys
   ```
   - `from=` must be the address the **server** sees the VM connect from (check `echo $SSH_CLIENT` in an ordinary login from the VM, or the server's auth log). The Mac's `10.211.55.*` is the Parallels network and is wrong here. Omit `from=` if that address is not stable; the forced command is still the boundary.
   - If sshd uses `AllowUsers`/`AllowGroups`, an admin must add gpuworker.
   - **Tailscale SSH bypasses this line.** On a server reached over Tailscale (or headscale) with Tailscale SSH on (`tailscale up --ssh`, `RunSSH: true`), `tailscaled` answers port 22 on the tailnet address and logs users in by tailnet identity: sshd and `authorized_keys` are never consulted, so the worker key gets a full shell and every `rjob` verb runs as a shell command (`rjob info` then prints the GNU `info` manual). Before turning it off (admin):
     - give every admin account its own key in its `~/.ssh/authorized_keys`, and test it (`ssh -i <key> <you>@127.0.0.1` on the server); password login is usually off, so without a key you are locked out;
     - check that sshd listens on all addresses (`ss -tln | grep ':22'`; on Ubuntu 24.04 `ssh.socket` starts sshd on demand);
     - keep a session open, run `sudo tailscale set --ssh=false`, and log in again from your laptop (`sudo tailscale set --ssh=true` from the open session or the provider's console undoes it);
     - the tailnet keeps working; only who answers port 22 changes. The headscale/Tailscale ACL must still allow TCP 22 from the VM's side.

     With Tailscale, `from=` is the address the server sees: the tailnet address of the machine the VM routes through (for a Parallels VM without its own Tailscale, the Mac's `100.x` address).
5. **Slots (optional).** Edit `~/rjob/config.toml`: `gpu_ids` (a subset reserves GPUs for other users), `cpu_slots`, `mem_gb`.
5a. **Paid cloud VM: autostop (admin).** Follow [power.md](power.md) so the VM stops itself through the provider API after 30 idle minutes. Don't use a cron `shutdown`: on Nebius a shutdown from inside the guest reboots the VM and keeps billing.

## VM

6. **Dedicated key per host.**
   ```bash
   ssh-keygen -t ed25519 -f ~/.ssh/rjob_gpuserver -N "" -C "vm-agents->gpuserver"
   cat ~/.ssh/rjob_gpuserver.pub
   ```
7. **SSH alias.** Append the `gpuserver` block of `vm/ssh_config.example` to `~/.ssh/config` with the real HostName.
8. **Project host list.** Add the alias to each project's `rjob.toml` (see `examples/project-rjob.toml`).

## Verify

```bash
rjob --host gpuserver info                     # devices include cuda; slots; free
rjob --project <project dir> hosts             # every project host, with free capacity
rjob submit examples/smoke-device-cuda         # placed on the CUDA host; result says device=cuda
```

**The lock must hold.** Each of these must fail or print only the usage line (if they succeed on a Tailscale host, see step 4):
```bash
ssh gpuserver
ssh gpuserver ls /home
scp ~/.bashrc gpuserver:
ssh -L 8080:localhost:80 gpuserver info
```

## Notes

- **GPU assignment.** Each GPU job gets concrete ids from `gpu_ids` (`resources.gpus`, default 1) and sees only them through `CUDA_VISIBLE_DEVICES`; CPU jobs get `CUDA_VISIBLE_DEVICES=""`. Inside the job, `cuda:0` is always its first assigned GPU. `provenance.json` records `gpu_ids`.
- **Memory.** `resources.mem_gb` is an estimate used only for scheduling; nothing enforces it. A job that exceeds it can push the host into swap or the OOM killer.
- **Reboots** kill jobs; they show `dead` and can be resubmitted.
- **Later: Slurm.** A Slurm backend would replace the host scheduler (`tick()` in `run_job.py`) with `sbatch`, behind the same job contract.
