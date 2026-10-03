from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Dict, Iterator, List, Optional

from . import config, proc

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


@dataclass(frozen=True)
class Agent:
    name: str
    runtime: str
    team: str = ""
    role: str = ""
    mode: str = "write"
    pid: int = 0
    pane_id: str = ""
    tmux_target: str = ""
    thread: str = ""
    cwd: str = ""
    worktree: str = ""
    depth: int = 0
    spawned_by: str = ""
    started: str = ""
    pid_start: str = ""
    reserved_until: float = 0.0
    extra: Dict[str, str] = field(default_factory=dict)

    @property
    def reserved(self) -> bool:
        return not self.pid and self.reserved_until > time.time()

    @property
    def live(self) -> bool:
        if not self.pid or not proc.alive(self.pid):
            return False
        return not self.pid_start or proc.start_time(self.pid) == self.pid_start

    @property
    def occupying(self) -> bool:
        return self.live or self.reserved


def path() -> Path:
    return config.state_dir() / "registry.json"


@contextlib.contextmanager
def _locked() -> Iterator[None]:
    lock = path().with_suffix(".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a") as fh:
        if fcntl:
            fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl:
                fcntl.flock(fh, fcntl.LOCK_UN)


def _read() -> Dict[str, Agent]:
    p = path()
    if not p.is_file():
        return {}
    try:
        raw = json.loads(p.read_text() or "{}")
    except ValueError:
        return {}
    known = set(Agent.__dataclass_fields__)
    return {name: Agent(**{k: v for k, v in data.items() if k in known}) for name, data in raw.get("agents", {}).items()}


def _write(agents: Dict[str, Agent]) -> None:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "agents": {n: asdict(a) for n, a in sorted(agents.items())}}
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".registry.")
    with os.fdopen(fd, "w") as fh:
        json.dump(payload, fh, indent=1)
    os.replace(tmp, p)


def all_agents() -> List[Agent]:
    return list(_read().values())


def get(name: str) -> Optional[Agent]:
    return _read().get(name)


def by_pid(pids: List[int]) -> Optional[Agent]:
    agents = _read().values()
    for pid in pids:
        for agent in agents:
            if agent.pid == pid:
                return agent
    return None


def by_thread(thread: str) -> Optional[Agent]:
    for agent in _read().values():
        if agent.thread == thread:
            return agent
    return None


def put(agent: Agent) -> None:
    with _locked():
        agents = _read()
        agents[agent.name] = agent
        _write(agents)


def update(name: str, **changes) -> Optional[Agent]:
    with _locked():
        agents = _read()
        if name not in agents:
            return None
        agents[name] = replace(agents[name], **changes)
        _write(agents)
        return agents[name]


def remove(names: List[str]) -> List[str]:
    with _locked():
        agents = _read()
        gone = [n for n in names if agents.pop(n, None) is not None]
        _write(agents)
        return gone


class Conflict(Exception):
    pass


RESERVATION_SECONDS = 300


def reserve(agent: Agent, check_writers: bool) -> None:
    """Atomically claim the name (and, for a writer, the worktree) before launching, so two concurrent spawns
    cannot both pass the checks. The placeholder has pid 0 and expires on its own if the spawner dies."""
    with _locked():
        agents = _read()
        held = agents.get(agent.name)
        if held and held.occupying:
            raise Conflict(f"the name '{agent.name}' is already taken by a live or launching agent")
        if check_writers:
            writers = [a for a in agents.values() if a.worktree == agent.worktree and a.mode == "write" and a.occupying]
            if writers:
                raise Conflict(", ".join(a.name for a in writers))
        agents[agent.name] = replace(agent, pid=0, reserved_until=time.time() + RESERVATION_SECONDS)
        _write(agents)


def release(name: str) -> None:
    with _locked():
        agents = _read()
        if name in agents and not agents[name].pid:
            agents.pop(name)
            _write(agents)


def gc() -> List[Agent]:
    with _locked():
        agents = _read()
        dead = [a for a in agents.values() if not a.occupying]
        for agent in dead:
            agents.pop(agent.name, None)
        _write(agents)
        return dead


def writers_in(worktree: str) -> List[Agent]:
    return [a for a in _read().values() if a.worktree == worktree and a.mode == "write" and a.occupying]
