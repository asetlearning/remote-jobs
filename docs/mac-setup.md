# Setup: Mac host + VM client

Some steps need an **admin on the Mac** and cannot be done from the VM. They are marked (Mac admin).

## Mac

1. **Worker account (Mac admin).** Create a Standard account with no Apple ID and nothing personal in it:
   ```bash
   sudo sysadminctl -addUser gpuworker -fullName "GPU worker" -password -
   ```
2. **Remote Login (Mac admin).** In System Settings → General → Sharing, turn on **Remote Login** and set "Allow access for: Only these users" to `gpuworker` only.
3. **Compiler (Mac admin).** Install the Xcode Command Line Tools with `xcode-select --install`. They provide git and clang++, and tcgraph needs C++20 `<format>`.
4. **Install the runner (as gpuworker).** The repo is visible on the Mac through the Parallels shared folder, or you can copy it:
   ```bash
   sudo -iu gpuworker
   bash /path/to/remote-jobs/host/install.sh
   ```
   - The script installs uv, Python 3.12, `~/rjob/venv` with torch, `~/rjob/bin/{run-job,run_job.py}` and `~/rjob/config.toml`.
   - It then checks clang++ `<format>` and torch MPS.
   - Re-run it after `host/run_job.py` changes.
5. **Authorized key (as gpuworker).** Use the VM public key from step 7:
   ```bash
   mkdir -p ~/.ssh && chmod 700 ~/.ssh
   # one line, see mac/authorized_keys.example:
   echo 'restrict,command="/Users/gpuworker/rjob/bin/run-job",from="10.211.55.*" ssh-ed25519 AAAA... vm-agents->gpuworker' >> ~/.ssh/authorized_keys
   chmod 600 ~/.ssh/authorized_keys
   ```
6. **Slots (optional).** Edit `~/rjob/config.toml`. The defaults for an 8-core M1 are `gpu_slots = 1` and `cpu_slots = 7`.

## VM

7. **Dedicated key.** Create it with no passphrase, because agents use it unattended. The forced command is the safety boundary.
   ```bash
   ssh-keygen -t ed25519 -f ~/.ssh/rjob_mac -N "" -C "vm-agents->gpuworker"
   cat ~/.ssh/rjob_mac.pub
   ```
8. **SSH alias.** `ip route | grep default` shows the Mac's address, usually `10.211.55.2`. Append `vm/ssh_config.example` to `~/.ssh/config` (Host `mac`). If the subnet differs, fix `from=` in step 5 too.
9. **Client.** `ln -s ~/Research/remote-jobs/client/rjob ~/.local/bin/rjob`. Requires Python 3.11 or later.

## Verify

```bash
rjob info                                  # host, cpus, mem, slots; default_python.mps == true
rjob submit examples/smoke-device && rjob wait <id> --interval 5 && rjob fetch <id>   # device=mps
rjob submit examples/b25-build             # first build: several minutes (torch + C++)
rjob submit examples/pb-smoke              # PatternBoost end to end on MPS
```

**The lock must hold.** Each of these must fail or print only the usage line:
```bash
ssh mac
ssh mac ls /Users
scp ~/.bashrc mac:
ssh -L 8080:localhost:80 mac info
```

## Notes

- **No GUI session.** MPS works over SSH without a GUI login, and the smoke test confirms it.
- **Sleep.** `caffeinate -i` keeps the Mac awake while a job runs. A reboot kills jobs; they then show `dead` and can be resubmitted.
- **Disk.** Builds (about 1–3 GB each, mostly the venv) and jobs accumulate. Clean them up with `rjob cleanup builds <days>` and `rjob cleanup job <id>`.
- **Hardening TODO.** Wrap job execution in `sandbox-exec`, denying network access and writes outside `~/rjob`, after checking that MPS still works under it.
