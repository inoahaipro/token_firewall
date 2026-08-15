import os
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = Path.home() / ".token-firewall.env"

env = os.environ.copy()
if ENV_FILE.exists():
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()

print("Starting Token Firewall (auto-restart)...", flush=True)

while True:
    proc = subprocess.run([sys.executable, "-u", str(BASE_DIR / "server.py")], env=env)
    print(f"server.py exited with code {proc.returncode}, restarting in 3s...", flush=True)
    time.sleep(3)
