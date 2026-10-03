from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from typing import List

from . import claude, codex, config, events, registry, tmux
from .identity import whoami


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    warn_only: bool = False


def _version(cmd: str) -> str:
    try:
        return subprocess.run([cmd, "--version"], capture_output=True, text=True, timeout=15).stdout.strip().splitlines()[0]
    except (OSError, subprocess.SubprocessError, IndexError):
        return "?"


def checks() -> List[Check]:
    out: List[Check] = []
    for tool in ("python3", "tmux", "git", "claude", "codex"):
        path = shutil.which(tool)
        out.append(Check(f"tool {tool}", bool(path), _version(tool) if path and tool in ("claude", "codex") else (path or "not on PATH"),
                         warn_only=tool in ("claude", "codex")))
    sess = claude.sessions()
    out.append(Check("claude sessions registry", claude.sessions_dir().is_dir(), f"{len(sess)} session files in {claude.sessions_dir()}"))
    protocols = {s.peer_protocol for s in sess if s.peer_protocol is not None}
    unknown = protocols - set(claude.KNOWN_PEER_PROTOCOLS)
    out.append(Check("claude peerProtocol", not unknown, f"seen {sorted(protocols) or 'none'}; bridge knows {list(claude.KNOWN_PEER_PROTOCOLS)}"))
    reachable = [s for s in sess if s.reachable]
    live = [s for s in sess if registry_pid_alive(s.pid)]
    out.append(Check("claude peer sockets", bool(reachable) or not live,
                     f"{len(reachable)} of {len(live)} live sessions reachable"
                     + ("" if reachable or not live else " — set CLAUDE_CODE_HARBOR_KITE=1 in Claude settings env"), warn_only=not live))
    harbor = os.environ.get("CLAUDE_CODE_HARBOR_KITE") == "1" or "CLAUDE_CODE_HARBOR_KITE" in _settings_text()
    out.append(Check("HARBOR_KITE configured", harbor, "settings.json env or current env" if harbor else "missing from ~/.claude/settings.json env"))
    blocked = [s for s in sess if s.reachable and s.blocked]
    out.append(Check("claude sessions blocked on a dialog", not blocked,
                     ", ".join(f"{s.name} ({s.waiting_for}, {s.tmux or 'no tmux'})" for s in blocked) or "none", warn_only=True))
    problems = codex.schema_problems() if codex.available() else ["codex not installed"]
    out.append(Check("codex state schema", not problems, "; ".join(problems) or f"{codex.state_db().name} ok", warn_only=not codex.available()))
    if codex.available():
        res = subprocess.run(["codex", "queue", "--help"], capture_output=True, text=True)
        out.append(Check("codex queue command", res.returncode == 0 and "--thread" in res.stdout, "present" if res.returncode == 0 else res.stderr.strip()[:120]))
    agents = registry.all_agents()
    dead = [a.name for a in agents if not a.live]
    out.append(Check("registry", not dead, f"{len(agents)} agents, {len(dead)} dead" + (" — run: bridge gc" if dead else ""), warn_only=True))
    try:
        events.emit("doctor")
        out.append(Check("event log writable", True, str(events.log_path())))
    except OSError as exc:
        out.append(Check("event log writable", False, str(exc)))
    return out


def registry_pid_alive(pid: int) -> bool:
    from . import proc
    return proc.alive(pid)


def _settings_text() -> str:
    path = config.claude_home() / "settings.json"
    try:
        return path.read_text()
    except OSError:
        return ""


def loopback(directory: str = ".", timeout: int = 180) -> Check:
    """Spawn a throwaway Codex, ask it to run `bridge pong <token>`, wait for that event, tear it down."""
    from . import spawn

    token = uuid.uuid4().hex[:12]
    name = f"doctor-{token[:6]}"
    with_tmp = spawn.Request(runtime="codex", dir=os.path.abspath(directory), name=name, team="doctor", shared=True, focus=False,
                             task=f"Run exactly this shell command and nothing else: bridge pong {token}")
    try:
        spawn.spawn(with_tmp)
    except spawn.SpawnError as exc:
        return Check("loopback claude->codex->bridge", False, f"spawn failed: {exc}")
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            if any(e.get("event") == "pong" and e.get("token") == token for e in events.read()):
                return Check("loopback claude->codex->bridge", True, f"codex '{name}' received the queue message and ran bridge")
            time.sleep(3)
        return Check("loopback claude->codex->bridge", False, f"no pong within {timeout}s; inspect: tmux window {name}")
    finally:
        try:
            spawn.stop(name, force=True)
        except spawn.SpawnError:
            pass


def render(results: List[Check]) -> int:
    worst = 0
    for c in results:
        mark = "ok  " if c.ok else ("warn" if c.warn_only else "FAIL")
        if not c.ok and not c.warn_only:
            worst = 1
        print(f"[{mark}] {c.name:<36} {c.detail}")
    me = whoami()
    print(f"\nyou are: {me.name + ' (' + me.source + ')' if me else 'unidentified (pass --from, or run inside an agent)'}")
    return worst
