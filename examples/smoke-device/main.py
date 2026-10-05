"""Device smoke test: reports the torch device and does one matmul; needs no repo.

Run with the host's default Python (install.sh puts torch in ~/rjob/venv).
"""
# Created by: Alex Myasnikov
# Date: October 5, 2026
import os

import torch

if torch.cuda.is_available():
    dev = "cuda"
elif torch.backends.mps.is_available():
    dev = "mps"
else:
    dev = "cpu"
print("device:", dev, flush=True)
x = torch.randn(4000, 4000, device=dev)
total = (x @ x).sum().item()
print("sum:", total, flush=True)
os.makedirs("outputs", exist_ok=True)
with open("outputs/result.txt", "w") as f:
    f.write(f"device={dev} sum={total}\n")
