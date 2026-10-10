"""Start the paper cross-exchange scanner.

Usage: python start_all.py
Paper only. No orders.
"""

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def port_busy(port: int = 8765) -> bool:
    import socket
    with socket.socket() as sock:
        sock.settimeout(0.4)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def start(cmd, name):
    print(f"[LAUNCH] Starting {name}...")
    return subprocess.Popen([sys.executable, cmd], cwd=ROOT)


if __name__ == "__main__":
    print("=== ARB PAPER LAUNCHER ===")
    print("Paper mode. No orders.")
    if port_busy():
        print("Already running at http://127.0.0.1:8765 — not starting a second scanner.")
        raise SystemExit(0)
    procs = [start("arb.py", "Cross-exchange ARB (port 8765)")]
    print("\nDashboard: http://127.0.0.1:8765")
    print("Press Ctrl+C to stop all engines.\n")
    
    try:
        while True:
            time.sleep(10)
    except KeyboardInterrupt:
        print("\n[LAUNCH] Shutting down all engines...")
        for p in procs:
            p.terminate()
        print("All stopped.")