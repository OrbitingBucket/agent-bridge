"""`bridge wait <peer>`: the orchestrator's timer for one handoff.

A timer is needed because a peer wedged on a permission prompt still accepts deliveries and never answers. A plain
sleep fires long after the reply has arrived and has to be cancelled by hand; this one ends as soon as there is
something to act on, and says what it is.

What it can see: bridge messages (the event log), the status Claude Code writes into its session file, a Codex pane
showing a dialog, and whether the peer's process is alive. It cannot see a Claude peer's native SendMessage, so for a
Claude peer the usual ending is IDLE: its turn is over, and its message, if it sent one, has already woken the caller."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, List, Tuple

from . import claude, codex, events, proc, registry, tmux
from .identity import IdentityConflict, whoami

REPLY, IDLE, BLOCKED, DEAD, TIMEOUT = "REPLY", "IDLE", "BLOCKED", "DEAD", "TIMEOUT"
EXIT = {REPLY: 0, IDLE: 0, DEAD: 3, BLOCKED: 12, TIMEOUT: 124}
BLOCKED_POLLS = 3  # a status can flicker; a dialog counts once it has been seen this many polls in a row
IDLE_POLLS = 2


class WaitError(Exception):
    def __init__(self, detail: str, code: int):
        super().__init__(detail)
        self.code = code


@dataclass(frozen=True)
class Outcome:
    state: str
    detail: str

    @property
    def exit_code(self) -> int:
        return EXIT[self.state]


def _delivered(rec: Dict, sender: str, to: str) -> bool:
    return (rec.get("event") == "send" and rec.get("outcome") == "delivered"
            and (not sender or rec.get("from") == sender) and (not to or rec.get("to") == to))


def _pending(records: List[Dict], peer: str, me: str) -> List[Dict]:
    """Messages from the peer already in the log that came after my last message to it. With no message of mine in
    the log there is nothing they could be answering, so nothing already there counts."""
    mine = [i for i, r in enumerate(records) if _delivered(r, me, peer)]
    if not mine:
        return []
    return [r for r in records[mine[-1] + 1:] if _delivered(r, peer, me)]


def _state(peer: str) -> Tuple[str, str]:
    """dead | blocked | idle | busy, from what the peer's runtime exposes. A Codex is never reported idle: its TUI
    gives no dependable signal, and a Codex can only answer through the bridge anyway."""
    agent = registry.get(peer)
    if agent and agent.runtime == "codex":
        if not agent.live:
            return "dead", f"its process is gone; restore it with: bridge resume {peer}"
        if agent.pane_id and codex.dialog_open(tmux.capture(agent.pane_id)):
            return "blocked", f"a dialog is open in {agent.tmux_target or agent.pane_id}"
        return "busy", ""
    live = [s for s in claude.sessions() if s.name == peer and proc.alive(s.pid)]
    if len(live) > 1:
        raise WaitError(f"{len(live)} live Claude sessions are named '{peer}'; rename one first", 6)
    if not live:
        return "dead", "no live Claude session has that name"
    if live[0].blocked:
        return "blocked", f"it is waiting for {live[0].waiting_for} in {live[0].tmux or 'its window'}"
    return ("idle" if live[0].status == "idle" else "busy"), ""


def wait(peer: str, timeout: float = 600, any_kind: bool = False, task: str = "", interval: float = 2.0,
         grace: float = 20.0) -> Outcome:
    try:
        me = whoami()
    except IdentityConflict:
        me = None
    me_name = me.name if me else ""
    state, detail = _state(peer)
    if state == "dead" and registry.get(peer) is None:
        raise WaitError(f"no agent named '{peer}' (bridge list shows who is there)", 2)

    def counts(rec: Dict) -> bool:
        return (any_kind or rec.get("kind") == "BATON") and (not task or rec.get("task") == task)

    # The cursor is taken before the read, so a record written in between is seen twice rather than missed.
    cursor = events.cursor()
    fresh = _pending(list(events.read()), peer, me_name)
    started = time.time()
    seen_busy = False
    blocked_polls = idle_polls = uncounted = 0
    while True:
        for rec in fresh:
            if counts(rec):
                return Outcome(REPLY, f"{peer} -> {rec.get('to')} kind={rec.get('kind')} task={rec.get('task', '-')} "
                                      f"msg={rec.get('msg', '-')} at {rec.get('ts', '?')}")
            uncounted += 1
        waited = time.time() - started
        state, detail = _state(peer)
        if state == "dead":
            return Outcome(DEAD, f"{peer} is not running: {detail}")
        blocked_polls = blocked_polls + 1 if state == "blocked" else 0
        idle_polls = idle_polls + 1 if state == "idle" else 0
        seen_busy = seen_busy or state == "busy"
        if blocked_polls >= BLOCKED_POLLS:
            return Outcome(BLOCKED, f"{peer} is stuck: {detail}. Only the human can clear it.")
        if idle_polls >= IDLE_POLLS and (seen_busy or waited >= grace):
            if seen_busy:
                return Outcome(IDLE, f"{peer} finished its turn. If no message from it reached you, it stopped without handing over.")
            return Outcome(IDLE, f"{peer} has been idle for the whole {int(waited)}s of this wait. If it has not reported, "
                                 f"your message may not have reached it.")
        if waited >= timeout:
            extra = f"; {uncounted} message(s) arrived that did not count (FYI or another task; --any counts FYI)" if uncounted else ""
            return Outcome(TIMEOUT, f"{peer}: no hand-over in {int(timeout)}s{extra}. It is {state}.")
        time.sleep(interval)
        new, cursor = events.tail(cursor)
        fresh = [r for r in new if _delivered(r, peer, me_name)]
