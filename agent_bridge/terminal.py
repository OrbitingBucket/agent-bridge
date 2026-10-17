"""One terminal tab per agent, attached to a private view of the tmux session.

A tab is just another tmux client. A grouped session (`view-<name>`) shares the agent session's windows but keeps
its own current window, so every tab can show a different agent while the bridge keeps managing the one session.
Supported terminals: kitty (remote control over the socket in KITTY_LISTEN_ON) and iTerm2 (AppleScript).
TERMINAL=auto picks the one the spawner runs in; `none` disables tabs; `kitty` / `iterm` force one.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

from . import config, tmux

KITTY_ENV = "KITTY_LISTEN_ON"


def say(msg: str) -> None:
    print(msg, file=sys.stderr)


def detect(cfg: Optional[config.Config] = None, env: Optional[Dict[str, str]] = None) -> str:
    """The terminal to open tabs in: the configured one, or for `auto` the one this process runs in (else none)."""
    cfg = config.load() if cfg is None else cfg
    env = dict(os.environ) if env is None else env
    choice = cfg.get("TERMINAL").strip().lower() or "none"
    if choice != "auto":
        return choice
    if env.get(KITTY_ENV) and shutil.which("kitty"):
        return "kitty"
    if env.get("TERM_PROGRAM") == "iTerm.app":
        return "iterm"
    return "none"


def view_session(name: str) -> str:
    return f"view-{name}"


def open_tab(name: str, pane_id: str, focus: bool = True, terminal: Optional[str] = None) -> bool:
    """Open a tab showing the agent's window, or bring the existing one forward. True when a tab shows it."""
    terminal = detect() if terminal is None else terminal
    if terminal not in ("kitty", "iterm"):
        return False
    view = _prepare_view(name, pane_id)
    if not view:
        return False
    attach = [tmux.binary(), "attach-session", "-t", view]
    if terminal == "kitty":
        return _open_kitty_tab(name, attach, focus)
    return _open_iterm_tab(attach)


def close_tab(name: str) -> bool:
    """Drop the agent's view session; the client attached to it exits and the terminal closes the tab."""
    view = view_session(name)
    if not tmux.has_session(view):
        return False
    tmux.kill_session(view)
    return True


def _prepare_view(name: str, pane_id: str) -> str:
    """The grouped session showing the agent's window, created on first use; '' when the window is gone."""
    target = tmux.window_target(pane_id)
    if not target:
        return ""
    session, _, window = target.partition(":")
    view = view_session(name)
    if not tmux.has_session(view):
        tmux.new_grouped_session(view, session)
    tmux.select_window(f"{view}:{window}")
    return view


def _kitty_socket() -> List[str]:
    """`--to` for kitty's remote control when KITTY_LISTEN_ON was not inherited: the newest kitty socket in /tmp."""
    if os.environ.get(KITTY_ENV):
        return []
    socks = []
    for path in Path("/tmp").glob("kitty-*"):
        try:
            if stat.S_ISSOCK(path.stat().st_mode):
                socks.append((path.stat().st_mtime, str(path)))
        except OSError:
            continue
    return ["--to", f"unix:{max(socks)[1]}"] if socks else []


def _kitty(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["kitty", "@", *_kitty_socket(), *args], capture_output=True, text=True)


def kitty_tab_exists(title: str) -> bool:
    res = _kitty("ls")
    if res.returncode != 0:
        return False
    try:
        windows = json.loads(res.stdout)
    except ValueError:
        return False
    return any(tab.get("title") == title for w in windows for tab in w.get("tabs", []))


def _open_kitty_tab(name: str, attach: List[str], focus: bool) -> bool:
    if kitty_tab_exists(name):
        if focus:
            _kitty("focus-tab", "--match", f"title:^{re.escape(name)}$")
        return True
    args = ["launch", "--type=tab", "--tab-title", name] + ([] if focus else ["--keep-focus"])
    res = _kitty(*args, *attach)
    if res.returncode != 0:
        say(f"note: no kitty tab for '{name}' ({res.stderr.strip() or 'kitty remote control unavailable'}); "
            f"it still runs in its tmux window")
        return False
    return True


def _open_iterm_tab(attach: List[str]) -> bool:
    cmd = " ".join(attach).replace('"', '\\"')
    script = (f'tell application "iTerm"\nactivate\nif (count of windows) = 0 then\n'
              f'create window with default profile command "{cmd}"\nelse\n'
              f'tell current window to create tab with default profile command "{cmd}"\nend if\nend tell')
    res = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if res.returncode != 0:
        say(f"note: no iTerm tab ({res.stderr.strip() or 'osascript failed'}); the agent still runs in its tmux window")
        return False
    return True
