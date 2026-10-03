from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Optional

from . import claude, proc, registry

IDENTITY_ENV = ("BRIDGE_NAME", "AGENT_SEND_FROM")
CODEX_CMD = re.compile(r"^(\S*/)?codex(\s|$)")


@dataclass(frozen=True)
class Me:
    name: str
    runtime: str
    team: str
    depth: int
    source: str


class IdentityConflict(Exception):
    pass


AUTHORITATIVE = ("registry", "claude-session")


def _resolve() -> Optional[Me]:
    rows = proc.table()
    chain = proc.ancestors(rows=rows)
    agents = registry.all_agents()
    for pid in chain:
        for agent in agents:
            if agent.pid == pid and agent.live:
                return Me(agent.name, agent.runtime, agent.team, agent.depth, "registry")
        sess = claude.session_for_pid(pid)
        if sess and sess.name:
            reg = registry.get(sess.name)
            return Me(sess.name, "claude", reg.team if reg else "", reg.depth if reg else 0, "claude-session")
    for var in IDENTITY_ENV:
        value = os.environ.get(var)
        if value:
            reg = registry.get(value)
            return Me(value, reg.runtime if reg else "external", reg.team if reg else "", reg.depth if reg else 0, f"env:{var}")
    return None


def whoami(explicit: str = "") -> Optional[Me]:
    """Resolve the calling agent, nearest ancestor first. Process ancestry beats inherited env, so a child spawned
    from another agent's shell never signs as its parent. --from only names a caller the bridge cannot identify;
    it can never override an identified caller (that would let an agent borrow another's name and team)."""
    actual = _resolve()
    if not explicit:
        return actual
    if actual and actual.source in AUTHORITATIVE:
        if explicit != actual.name:
            raise IdentityConflict(f"you are '{actual.name}' (from {actual.source}); --from {explicit} cannot override that")
        return actual
    if registry.get(explicit) and registry.get(explicit).live:
        raise IdentityConflict(f"'{explicit}' is a registered live agent and this process is not it; --from cannot borrow its identity")
    runtime = actual.runtime if actual else "external"
    return Me(explicit, runtime, "", 0, "flag")


def depth() -> int:
    me = whoami()
    env_depth = int(os.environ.get("AGENT_BRIDGE_DEPTH", "0") or 0)
    return max(me.depth if me else 0, env_depth)
