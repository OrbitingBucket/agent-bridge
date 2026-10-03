from __future__ import annotations

import os
import subprocess
from typing import Dict, List, Optional


def alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def table() -> Dict[int, tuple]:
    """pid -> (ppid, command) for every process; empty if ps is unavailable (e.g. a sandbox)."""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid=,command="], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    rows: Dict[int, tuple] = {}
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            rows[int(parts[0])] = (int(parts[1]), parts[2] if len(parts) > 2 else "")
    return rows


def ancestors(pid: Optional[int] = None, rows: Optional[Dict[int, tuple]] = None) -> List[int]:
    pid = pid or os.getpid()
    rows = rows if rows is not None else table()
    chain: List[int] = []
    seen = set()
    while pid and pid not in seen and pid > 1:
        seen.add(pid)
        chain.append(pid)
        pid = rows.get(pid, (0, ""))[0]
    if not rows:
        chain.append(os.getppid())
    return chain


def cwd_of(pid: int) -> Optional[str]:
    try:
        out = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        if line.startswith("n"):
            return line[1:]
    return None


def start_time(pid) -> str:
    """Process start stamp; pairs with the pid so a recycled pid is never mistaken for the original process."""
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(int(pid))], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""
    return " ".join(out.split())
