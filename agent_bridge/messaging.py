from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import claude, codex, config, events, gitutil, marker, registry
from .identity import IdentityConflict, Me, whoami

EXIT = {
    "unresolved": 2,
    "dead": 3,
    "queue_failed": 5,
    "ambiguous": 6,
    "cross_team": 7,
    "eperm": 8,
    "refused": 8,
    "timeout": 8,
    "socket_error": 8,
    "no_identity": 9,
    "identity_conflict": 9,
    "invalid_field": 64,
    "spill_failed": 8,
}


class SendError(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code

    @property
    def exit_code(self) -> int:
        return EXIT.get(self.code, 1)


@dataclass(frozen=True)
class Target:
    name: str
    runtime: str
    team: str
    cwd: str
    session: Optional[claude.Session] = None
    thread: str = ""
    liveness: str = ""


def _codex_liveness(thread: codex.Thread, force: bool) -> str:
    agent = registry.by_thread(thread.id)
    if agent and agent.live:
        return f"registry pid {agent.pid}"
    if force:
        return "forced"
    hint = f"bridge resume {thread.name or thread.id}"
    if agent:
        raise SendError("dead", f"Codex '{thread.name or thread.id}' is not running (its registered process is gone); a queued message would only run after a resume. Restore it with: {hint}")
    raise SendError("dead", f"Codex '{thread.name or thread.id}' is not a registered bridge agent, so the bridge cannot prove it is running. "
                            f"Restore it under the bridge with: {hint} — or pass --force to queue anyway")


def _claude_target(name: str, team: str) -> Optional[Target]:
    found = claude.find(name)
    if len(found) > 1:
        pids = ", ".join(str(s.pid) for s in found)
        raise SendError("ambiguous", f"{len(found)} live Claude sessions are named '{name}' (pids {pids}); rename one before messaging")
    if not found:
        return None
    s = found[0]
    return Target(name, "claude", team, s.cwd, session=s, liveness=f"socket {s.socket_path}")


def _codex_thread(name: str, agent: Optional[registry.Agent]) -> Optional[codex.Thread]:
    if codex.UUID_RE.match(name):
        return codex.thread_by_id(name) or codex.Thread(name, name, "")
    if agent and agent.runtime == "codex" and agent.thread:
        return codex.thread_by_id(agent.thread) or codex.Thread(agent.thread, name, agent.cwd)
    named = codex.threads_named(name)
    if len(named) > 1:
        raise SendError("ambiguous", f"{len(named)} Codex threads are named '{name}'; send to the thread uuid instead")
    return named[0] if named else None


def resolve(name: str, runtime: str = "", force: bool = False) -> Target:
    """Both runtimes are always checked; a name that exists in both is refused unless --runtime picks one."""
    agent = registry.get(name)
    team = agent.team if agent else ""
    claude_t = _claude_target(name, team) if runtime in ("", "claude") and not codex.UUID_RE.match(name) else None
    thread = _codex_thread(name, agent) if runtime in ("", "codex") else None
    if claude_t and thread and not runtime:
        raise SendError("ambiguous", f"'{name}' is both a live Claude session and a Codex thread; pass --runtime claude|codex")
    if claude_t:
        return claude_t
    if runtime == "claude":
        raise SendError("dead", f"no reachable Claude session named '{name}' (bridge list shows who is reachable)")
    if thread is None:
        raise SendError("unresolved", f"no agent named '{name}': no reachable Claude session and no Codex thread with that name")
    owner = registry.by_thread(thread.id)
    return Target(thread.name or name, "codex", owner.team if owner else team, thread.cwd, thread=thread.id,
                  liveness=_codex_liveness(thread, force))


def _spill(target: Target, msg_id: str, text: str, preview: int) -> tuple:
    try:
        base = gitutil.relay_dir(target.cwd, "spill") if target.cwd and Path(target.cwd).is_dir() else config.state_dir() / "spill"
        base.mkdir(parents=True, exist_ok=True)
        path = base / f"{msg_id}.md"
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
    except OSError as exc:
        raise SendError("spill_failed", f"could not write the long message to a file ({exc}); if a sandbox denied it, rerun with escalation") from exc
    head = text.encode()[:preview].decode(errors="ignore")
    body = f"[full message ({len(text.encode())} bytes) is in the file {path} — read it before acting]\n{head}…"
    return body, str(path)


def send(to: str, text: str, kind: str = "BATON", task: str = "-", sender: str = "", runtime: str = "",
         force: bool = False, cross_team: bool = False) -> dict:
    cfg = config.load()
    started = time.time()
    data = text.encode()
    sha = hashlib.sha256(data).hexdigest()[:16]
    base = {"to": to, "kind": kind, "task": task, "bytes": len(data), "sha": sha}
    me: Optional[Me] = None
    try:
        if kind not in marker.KINDS:
            raise SendError("unresolved", f"--kind must be one of {', '.join(marker.KINDS)}")
        try:
            me = whoami(sender)
        except IdentityConflict as exc:
            raise SendError("identity_conflict", str(exc)) from exc
        if me is None:
            raise SendError("no_identity", "cannot tell who is sending: not a registered agent, not inside a Claude session, no BRIDGE_NAME. Pass --from <your-name> — for a Codex session, its thread name (set with /rename) so replies can reach you")
        target = resolve(to, runtime, force)
        if me.team and target.team and me.team != target.team and not cross_team:
            raise SendError("cross_team", f"'{me.name}' (team {me.team}) is messaging '{target.name}' (team {target.team}); pass --cross-team if intended")
        label = target.name if marker.SAFE_VALUE.match(target.name) else (target.thread or "unnamed")
        env = marker.Envelope(me.name, f"{target.runtime}:{label}", task, kind, me.team or target.team or "-").with_id()
        try:
            marker.header(env, False)
        except marker.InvalidField as exc:
            raise SendError("invalid_field", str(exc)) from exc
        spilled = None
        if len(data) > cfg.int("SOFT_CAP"):
            text, spilled = _spill(target, env.msg, text, cfg.int("SPILL_PREVIEW"))
        if target.runtime == "claude":
            claude.deliver(target.session, marker.wrap(env, text, human_note=True))
            transport = "claude-socket"
        else:
            codex.queue(target.thread, marker.wrap(env, text, human_note=False))
            transport = "codex-queue"
    except SendError as exc:
        events.emit("send", **base, outcome="error", error=exc.code, detail=str(exc)[:300], **{"from": me.name if me else None})
        raise
    except claude.TransportError as exc:
        events.emit("send", **base, outcome="error", error=exc.code, detail=str(exc)[:300], **{"from": me.name if me else None})
        raise SendError(exc.code, str(exc)) from exc
    except codex.QueueError as exc:
        events.emit("send", **base, outcome="error", error="queue_failed", detail=str(exc)[:300], **{"from": me.name if me else None})
        raise SendError("queue_failed", str(exc)) from exc
    record = events.emit(
        "send", **base, outcome="delivered", msg=env.msg, team=env.team, to_runtime=target.runtime,
        from_runtime=me.runtime, transport=transport, liveness=target.liveness, spilled=spilled,
        latency_ms=int((time.time() - started) * 1000), **{"from": me.name},
    )
    return record
