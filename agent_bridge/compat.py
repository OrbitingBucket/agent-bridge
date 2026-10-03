"""Old command names (agent-send, codex-send, codex-relay, claude-relay, team-up, agent-kick) mapped onto bridge.

Agents, skills and memories written before v1 keep working unchanged."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from typing import List

from . import cli


def _agent_send(argv: List[str]) -> int:
    if not argv or argv[0] == "--list":
        return cli.cmd_list(None)
    env_defaults = {
        "kind": os.environ.get("AGENT_SEND_KIND", "BATON"),
        "task": os.environ.get("AGENT_SEND_TASK", "-"),
        "sender": "",
    }
    args = cli.parse_send(argv, env_defaults)
    args.runtime = args.runtime or "claude"
    return cli.cmd_send(args)


def _codex_send(argv: List[str]) -> int:
    if argv and argv[0] in ("-h", "--help"):
        print("codex-send [--task ID] [--kind BATON|FYI] [--from NAME] [--force] <name|uuid> <text...>  (alias of: bridge send --runtime codex)")
        return 0
    args = cli.parse_send(argv)
    args.runtime = args.runtime or "codex"
    return cli.cmd_send(args)


def _spawn_ns(**kw) -> argparse.Namespace:
    base = dict(here=False, dir=".", name="", role="builder", team="", peer="", task="", task_file="", profile="", model="",
                effort="", permission_mode="", worktree="", shared=False, no_focus=False, extra=[])
    base.update(kw)
    return argparse.Namespace(**base)


def _codex_relay(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(prog="codex-relay")
    ap.add_argument("-d", "--dir", default=".")
    ap.add_argument("-n", "--name", default="")
    ap.add_argument("-s", "--session", default="")
    ap.add_argument("-p", "--profile", default="")
    ap.add_argument("-t", "--task", default="")
    ap.add_argument("--resume", default="")
    ap.add_argument("--tab", action="store_true")
    ap.add_argument("--no-tab", action="store_true")
    ap.add_argument("--no-focus", action="store_true")
    ap.add_argument("--shared", action="store_true")
    ap.add_argument("extra", nargs="*")
    a = ap.parse_args(argv)
    if a.session:
        os.environ["BRIDGE_TMUX_SESSION"] = a.session
    if a.tab:
        os.environ["BRIDGE_TERMINAL"] = "iterm"
    if a.resume:
        return cli.cmd_resume(argparse.Namespace(target=a.resume, profile=a.profile, task=a.task, no_focus=a.no_focus))
    ns = _spawn_ns(dir=a.dir, name=a.name, profile=a.profile, task=a.task, no_focus=a.no_focus, shared=a.shared, extra=a.extra)
    return cli.cmd_spawn(ns, runtime="codex")


def _claude_relay(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(prog="claude-relay")
    ap.add_argument("role", choices=["orchestrator", "builder"])
    ap.add_argument("-d", "--dir", default=".")
    ap.add_argument("-n", "--name", default="")
    ap.add_argument("-p", "--peer", default="")
    ap.add_argument("-t", "--task", default="")
    ap.add_argument("-m", "--model", default="")
    ap.add_argument("--mode", default="")
    ap.add_argument("-s", "--session", default="")
    ap.add_argument("--team", default="")
    ap.add_argument("--tab", action="store_true")
    ap.add_argument("--no-tab", action="store_true")
    ap.add_argument("--no-focus", action="store_true")
    ap.add_argument("--shared", action="store_true")
    ap.add_argument("--here", action="store_true")
    a = ap.parse_args(argv)
    if a.session:
        os.environ["BRIDGE_TMUX_SESSION"] = a.session
    if a.tab:
        os.environ["BRIDGE_TERMINAL"] = "iterm"
    peer = a.peer or ("builder" if a.role == "orchestrator" else "orchestrator")
    ns = _spawn_ns(dir=a.dir, name=a.name or a.role, role=a.role, here=a.here, peer=peer, task=a.task, model=a.model,
                   permission_mode=a.mode, team=a.team, no_focus=a.no_focus, shared=a.shared)
    return cli.cmd_spawn(ns, runtime="claude")


def _team_up(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(prog="team-up")
    ap.add_argument("-d", "--dir", required=True)
    ap.add_argument("-s", "--session", default="")
    ap.add_argument("-b", "--builders", type=int, default=1)
    ap.add_argument("--codex", action="store_true")
    ap.add_argument("-p", "--profile", default="")
    ap.add_argument("-t", "--task", default="")
    ap.add_argument("-n", "--name", default="")
    ap.add_argument("--tab", action="store_true")
    ap.add_argument("--no-tab", action="store_true")
    ap.add_argument("--worktrees", action="store_true")
    a = ap.parse_args(argv)
    if a.session:
        os.environ["BRIDGE_TMUX_SESSION"] = a.session
    if a.tab:
        os.environ["BRIDGE_TERMINAL"] = "iterm"
    return cli.cmd_team(argparse.Namespace(dir=a.dir, builders=a.builders, codex=a.codex, profile=a.profile,
                                           task=a.task, team=a.name, worktrees=a.worktrees))


def _agent_kick(argv: List[str]) -> int:
    if len(argv) < 2:
        print("usage: agent-kick <agent-name|tmux-target> <text...>", file=sys.stderr)
        return 64
    from . import registry
    if registry.get(argv[0]):
        return cli.cmd_kick(argparse.Namespace(name=argv[0], text=argv[1:]))
    pane = subprocess.run(["tmux", "display-message", "-p", "-t", argv[0], "#{pane_id}"], capture_output=True, text=True).stdout.strip()
    owner = next((a for a in registry.all_agents() if pane and a.pane_id == pane), None)
    if owner:
        return cli.cmd_kick(argparse.Namespace(name=owner.name, text=argv[1:]))
    print(f"agent-kick: '{argv[0]}' is not a registered agent's pane; refusing to type into an unverified target "
          f"(window indexes shift when windows close). Use: bridge kick <agent-name> ...", file=sys.stderr)
    return 3


def run(prog: str, argv: List[str]) -> int:
    return {
        "agent-send": _agent_send,
        "codex-send": _codex_send,
        "codex-relay": _codex_relay,
        "claude-relay": _claude_relay,
        "team-up": _team_up,
        "agent-kick": _agent_kick,
    }[prog](argv)
