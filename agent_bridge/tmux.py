from __future__ import annotations

import os
import shutil
import subprocess
import time
from typing import List, Optional


def available() -> bool:
    return shutil.which("tmux") is not None


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", *args], capture_output=True, text=True)


def inside() -> bool:
    return bool(os.environ.get("TMUX"))


def current_session() -> str:
    return _run("display-message", "-p", "#S").stdout.strip()


def has_session(name: str) -> bool:
    return _run("has-session", "-t", name).returncode == 0


def new_window(session: str, name: str, command: str) -> str:
    """Create a window running `command` and return its STABLE pane id (%N)."""
    fmt = "#{pane_id}"
    window = ("new-window", "-d", "-P", "-F", fmt, "-t", f"{session}:", "-n", name, command)
    if has_session(session):
        res = _run(*window)
    else:
        res = _run("new-session", "-d", "-P", "-F", fmt, "-s", session, "-n", name, "-x", "220", "-y", "50", command)
        if res.returncode != 0 and has_session(session):  # a concurrent spawn created the session first
            res = _run(*window)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or "tmux failed to create the window")
    return res.stdout.strip()


def pane_pid(pane_id: str) -> Optional[int]:
    res = _run("display-message", "-p", "-t", pane_id, "#{pane_pid}")
    out = res.stdout.strip()
    return int(out) if res.returncode == 0 and out.isdigit() else None


def pane_alive(pane_id: str) -> bool:
    return bool(pane_id) and pane_pid(pane_id) is not None


def capture(pane_id: str) -> str:
    return _run("capture-pane", "-p", "-t", pane_id).stdout


def window_target(pane_id: str) -> str:
    return _run("display-message", "-p", "-t", pane_id, "#{session_name}:#{window_name}").stdout.strip()


def kill(pane_id: str) -> None:
    _run("kill-pane", "-t", pane_id)


def type_text(pane_id: str, text: str) -> None:
    """Two-step send: a same-call Enter races the paste and leaves the text unsubmitted."""
    _run("send-keys", "-t", pane_id, "-l", text)
    time.sleep(1)
    _run("send-keys", "-t", pane_id, "Enter")


ALERT_STYLE = "bg=red,fg=white,bold"


def alert(pane_id: str, text: str, seconds: int = 20) -> None:
    """Tell the human a window needs them without moving their view: a status-line message on every attached client,
    and the window's name highlighted in the status bar until clear_alert()."""
    _run("set-option", "-w", "-t", pane_id, "window-status-style", ALERT_STYLE)
    literal = text.replace("#", "##")  # display-message expands #{...}; an agent or window name must not be read as a format
    for client in _run("list-clients", "-F", "#{client_name}").stdout.splitlines():
        if client:
            _run("display-message", "-c", client, "-d", str(seconds * 1000), literal)


def clear_alert(pane_id: str) -> None:
    _run("set-option", "-w", "-u", "-t", pane_id, "window-status-style")


def focus(pane_id: str) -> None:
    """Bring the pane's window to the front of every attached client viewing its session group."""
    target = window_target(pane_id)
    if not target:
        return
    session, _, window = target.partition(":")
    group = _run("display-message", "-p", "-t", session, "#{session_group}").stdout.strip()
    clients = _run("list-clients", "-F", "#{session_name} #{session_group}").stdout.splitlines()
    sessions: List[str] = []
    for line in clients:
        parts = line.split(" ")
        cs, cg = parts[0], (parts[1] if len(parts) > 1 else "")
        if cs == session or (group and cg == group):
            sessions.append(cs)
    for cs in set(sessions):
        _run("select-window", "-t", f"{cs}:{window}")
    _run("select-window", "-t", pane_id)
