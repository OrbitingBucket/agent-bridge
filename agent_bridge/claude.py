from __future__ import annotations

import errno
import json
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from . import config, proc

KNOWN_PEER_PROTOCOLS = (1,)


@dataclass(frozen=True)
class Session:
    name: str
    pid: int
    cwd: str
    socket_path: str
    started_at: int
    peer_protocol: Optional[int]
    tmux: str = ""
    status: str = ""
    waiting_for: str = ""

    @property
    def reachable(self) -> bool:
        return bool(self.socket_path) and os.path.exists(self.socket_path) and proc.alive(self.pid)

    @property
    def blocked(self) -> bool:
        return self.status == "waiting" and bool(self.waiting_for)


class TransportError(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code


def sessions_dir() -> Path:
    return config.claude_home() / "sessions"


def _load(path: Path) -> Optional[Session]:
    try:
        d = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or "pid" not in d:
        return None
    return Session(
        name=d.get("name") or "",
        pid=int(d.get("pid") or 0),
        cwd=d.get("cwd") or "",
        socket_path=d.get("messagingSocketPath") or "",
        started_at=int(d.get("startedAt") or 0),
        peer_protocol=d.get("peerProtocol"),
        tmux=d.get("tmux") or "",
        status=d.get("status") or "",
        waiting_for=d.get("waitingFor") or "",
    )


def sessions() -> List[Session]:
    out = []
    for f in sessions_dir().glob("*.json"):
        s = _load(f)
        if s:
            out.append(s)
    return out


def session_for_pid(pid: int) -> Optional[Session]:
    return _load(sessions_dir() / f"{pid}.json")


def find(name: str) -> List[Session]:
    """Every REACHABLE session with exactly this name (more than one = ambiguous, never guessed)."""
    return [s for s in sessions() if s.name == name and s.reachable]


def deliver(sess: Session, content: str, timeout: float = 5.0) -> None:
    payload = json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(sess.socket_path)
        sock.sendall(payload.encode())
    except PermissionError as exc:
        raise TransportError("eperm", f"{exc} — a sandbox denied the unix-socket connect; rerun this command with sandbox escalation") from exc
    except socket.timeout as exc:
        raise TransportError("timeout", str(exc)) from exc
    except OSError as exc:
        code = "refused" if exc.errno in (errno.ECONNREFUSED, errno.ENOENT) else "socket_error"
        raise TransportError(code, str(exc)) from exc
    finally:
        sock.close()
