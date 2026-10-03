from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional

from . import claude, config, doctor, events, messaging, registry, spawn, trust
from .identity import whoami

COMPAT = {"agent-send", "codex-send", "codex-relay", "claude-relay", "team-up", "agent-kick"}
EFFORT_HELP = "reasoning effort for this lane, e.g. low|medium|high (default: CLAUDE_EFFORT / CODEX_EFFORT, else your own setting)"


SEND_VALUE_FLAGS = {"--kind": "kind", "--task": "task", "--from": "sender", "--runtime": "runtime", "--file": "file"}
SEND_BOOL_FLAGS = {"--force": "force", "--cross-team": "cross_team"}


class SendArgs(argparse.Namespace):
    pass


def parse_send(argv: List[str], defaults: Optional[dict] = None) -> SendArgs:
    """Flags may come before or after the recipient (both habits exist in the wild); everything after the first
    plain token that follows the recipient is message text, verbatim. A trailing `--kind X` / `--task X` is also
    accepted, because agents copy that shape from codex-send."""
    args = SendArgs(kind="BATON", task="-", sender="", runtime="", file=None, force=False, cross_team=False, to=None, text=[])
    for k, v in (defaults or {}).items():
        setattr(args, k, v)
    tokens = list(argv)
    i = 0
    literal = False
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--":
            i += 1
            literal = True
            break
        if tok in SEND_VALUE_FLAGS and i + 1 < len(tokens):
            setattr(args, SEND_VALUE_FLAGS[tok], tokens[i + 1])
            i += 2
            continue
        if "=" in tok and tok.split("=", 1)[0] in SEND_VALUE_FLAGS:
            key, _, value = tok.partition("=")
            setattr(args, SEND_VALUE_FLAGS[key], value)
            i += 1
            continue
        if tok in SEND_BOOL_FLAGS:
            setattr(args, SEND_BOOL_FLAGS[tok], True)
            i += 1
            continue
        if args.to is None:
            args.to = tok
            i += 1
            continue
        break
    rest = tokens[i:]
    if args.to is None and rest:
        args.to, rest = rest[0], rest[1:]
    while not literal and len(rest) >= 3 and rest[-2] in ("--kind", "--task"):
        setattr(args, SEND_VALUE_FLAGS[rest[-2]], rest[-1])
        rest = rest[:-2]
    args.text = rest
    args.kind = (args.kind or "BATON").upper()
    return args


def _add_spawn(p: argparse.ArgumentParser, runtime: Optional[str]) -> None:
    if runtime is None:
        p.add_argument("runtime", choices=["claude", "codex"])
    p.add_argument("-d", "--dir", default=".")
    p.add_argument("-n", "--name", default="")
    p.add_argument("-r", "--role", default="builder", choices=["builder", "orchestrator"])
    p.add_argument("--team", default="")
    p.add_argument("--peer", default="")
    p.add_argument("-t", "--task", default="", help="the first brief; the agent starts on it without a round trip")
    p.add_argument("--task-file", default="", help="read the brief from a file, or - for stdin (no shell quoting to get wrong)")
    p.add_argument("-p", "--profile", default="", help="Codex profile (~/.codex/<profile>.config.toml)")
    p.add_argument("-m", "--model", default="")
    p.add_argument("--effort", default="", help=EFFORT_HELP)
    p.add_argument("--mode", dest="permission_mode", default="", help="Claude --permission-mode (default: inherit)")
    p.add_argument("--worktree", default="", help="create/reuse an isolated git worktree on this branch")
    p.add_argument("--shared", action="store_true", help="allow a second writer in the same worktree")
    p.add_argument("--no-focus", action="store_true")
    p.add_argument("extra", nargs="*", help="after --: extra args for claude/codex")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="bridge", description="Local message bridge for Claude Code and Codex CLI agents.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("send", help="bridge send [--kind BATON|FYI] [--task ID] [--from NAME] [--runtime claude|codex] [--force] [--cross-team] [--file PATH|-] <to> <text...>")
    _add_spawn(sub.add_parser("spawn", help="launch a Claude or Codex agent in tmux"), None)
    r = sub.add_parser("resume", help="restore a closed Codex peer with its history")
    r.add_argument("target")
    r.add_argument("-p", "--profile", default="")
    r.add_argument("-t", "--task", default="")
    r.add_argument("--effort", default="", help="reasoning effort (default: the effort it was spawned with)")
    r.add_argument("--no-focus", action="store_true")
    r.add_argument("--shared", action="store_true", help="allow it back into a worktree another writer now holds")
    t = sub.add_parser("team", help="orchestrator + builders (+ Codex) in one command")
    t.add_argument("-d", "--dir", required=True)
    t.add_argument("-b", "--builders", type=int, default=1)
    t.add_argument("--codex", action="store_true")
    t.add_argument("-p", "--profile", default="")
    t.add_argument("-t", "--task", default="")
    t.add_argument("--task-file", default="", help="read the objective from a file, or - for stdin")
    t.add_argument("--effort", default="", help="reasoning effort for the builders and Codex (the orchestrator keeps the default)")
    t.add_argument("--team", default="")
    t.add_argument("--worktrees", action="store_true", help="give every writing builder its own git worktree")
    s = sub.add_parser("stop", help="tear down an agent you spawned (kills its window, unregisters it)")
    s.add_argument("name")
    s.add_argument("--force", action="store_true")
    sub.add_parser("list", help="agents, reachability, blocked dialogs")
    g = sub.add_parser("gc", help="drop registry entries whose process is gone")
    g.add_argument("--dry-run", action="store_true")
    d = sub.add_parser("doctor", help="check every dependency the bridge relies on")
    d.add_argument("--loopback", action="store_true", help="also spawn a throwaway Codex and round-trip a message (spends tokens)")
    d.add_argument("--dir", default=".", help="directory for the loopback Codex (must be one Codex already trusts)")
    e = sub.add_parser("events", help="read the event log")
    e.add_argument("--stalls", action="store_true", help="STALL / LOOP / BLOAT / ERRORS readout")
    e.add_argument("--tail", type=int, default=20)
    k = sub.add_parser("kick", help="last resort: type into a registered agent's pane (verified by pid)")
    k.add_argument("name")
    k.add_argument("text", nargs=argparse.REMAINDER)
    sub.add_parser("whoami")
    sub.add_parser("import-legacy", help="import live entries from ~/.claude/relay/bridge-registry.conf (v0)")
    tr = sub.add_parser("trust", help="human only: approve a folder for Codex once, so spawns there skip its trust dialog")
    tr.add_argument("dir", nargs="?", default=".")
    tr.add_argument("--dry-run", action="store_true")
    pong = sub.add_parser("pong")
    pong.add_argument("token")
    return ap


def _text(args) -> str:
    if args.file:
        return sys.stdin.read() if args.file == "-" else open(args.file).read()
    return " ".join(args.text)


def cmd_send(args) -> int:
    if not args.to:
        print("usage: bridge send [--kind BATON|FYI] [--task ID] [--from NAME] <to> <text...>", file=sys.stderr)
        return 64
    if args.kind not in ("BATON", "FYI"):
        print(f"bridge send: --kind must be BATON or FYI (got {args.kind})", file=sys.stderr)
        return 64
    body = _text(args)
    if not body.strip():
        print("bridge send: empty message", file=sys.stderr)
        return 64
    try:
        rec = messaging.send(args.to, body, kind=args.kind, task=args.task, sender=args.sender, runtime=args.runtime,
                             force=args.force, cross_team=args.cross_team)
    except messaging.SendError as exc:
        print(f"bridge send: {exc}", file=sys.stderr)
        return exc.exit_code
    extra = f" spilled={rec['spilled']}" if rec.get("spilled") else ""
    print(f"delivered -> {rec['to_runtime']}:{rec['to']} kind={rec['kind']} task={rec['task']} msg={rec['msg']} [{rec['liveness']}]{extra}")
    return 0


class UsageError(Exception):
    pass


def _task(args) -> str:
    """The brief: -t, or --task-file for one that is long or full of characters a shell would mangle."""
    source = getattr(args, "task_file", "")
    if not source:
        return args.task
    if args.task:
        raise UsageError("pass the brief with -t or with --task-file, not both")
    try:
        text = sys.stdin.read() if source == "-" else open(source).read()
    except OSError as exc:
        raise UsageError(f"cannot read --task-file: {exc}") from exc
    if not text.strip():
        raise UsageError(f"--task-file {source} is empty")
    return text.strip()


def _spawn_request(args, runtime: str) -> spawn.Request:
    return spawn.Request(
        runtime=runtime, dir=args.dir, name=args.name, role=args.role, team=args.team, peer=args.peer, task=_task(args),
        profile=args.profile, model=args.model, effort=getattr(args, "effort", ""),
        permission_mode=args.permission_mode, worktree=args.worktree,
        shared=args.shared, focus=False if args.no_focus else None, extra_args=tuple(args.extra),
        here=getattr(args, "here", False),
    )


def _report(agent) -> None:
    effort = f" effort={agent.extra['effort']}" if agent.extra.get("effort") else ""
    print(f"launched: {agent.runtime} '{agent.name}' team={agent.team} role={agent.role} mode={agent.mode} depth={agent.depth}{effort}")
    print(f"  cwd   : {agent.cwd}")
    print(f"  window: {agent.tmux_target} ({agent.pane_id})")
    if agent.thread:
        print(f"  thread: {agent.thread}")
    print(f"  reach : bridge send {agent.name} \"<text>\"")


def cmd_spawn(args, runtime: Optional[str] = None) -> int:
    try:
        agent = spawn.spawn(_spawn_request(args, runtime or args.runtime))
    except UsageError as exc:
        print(f"bridge spawn: {exc}", file=sys.stderr)
        return 64
    except spawn.SpawnError as exc:
        print(f"bridge spawn: {exc}", file=sys.stderr)
        return exc.code
    _report(agent)
    return 0


def cmd_resume(args) -> int:
    req = spawn.Request(runtime="codex", dir=".", resume=args.target, profile=args.profile, task=args.task,
                        effort=getattr(args, "effort", ""),
                        shared=getattr(args, "shared", False), focus=False if args.no_focus else None)
    try:
        agent = spawn.spawn(req)
    except spawn.SpawnError as exc:
        print(f"bridge resume: {exc}", file=sys.stderr)
        return exc.code
    _report(agent)
    return 0


def cmd_team(args) -> int:
    import os
    from pathlib import Path
    from . import gitutil

    try:
        objective = _task(args)
    except UsageError as exc:
        print(f"bridge team: {exc}", file=sys.stderr)
        return 64
    effort = getattr(args, "effort", "")
    base = args.team or Path(gitutil.toplevel(args.dir) or os.path.abspath(args.dir)).name
    roster = [f"{base}-bld{i} = Claude builder (SendMessage)" for i in range(1, args.builders + 1)]
    if args.codex:
        roster.append(f"{base}-cdx = Codex (bridge send {base}-cdx \"<brief>\"; it replies with bridge send)")
    task = "Your team (assign DISJOINT lanes, one brief each): " + "; ".join(roster) + (f". Objective: {objective}" if objective else "")
    writers = args.builders + (1 if args.codex and (args.profile or config.load().get("CODEX_PROFILE")) not in config.load().get("CODEX_READONLY_PROFILES").split() else 0)
    if writers > 1 and not args.worktrees:
        print(f"warning: {writers} writing agents will share {args.dir}; consider --worktrees", file=sys.stderr)
    orchestrator = spawn.Request(runtime="claude", dir=args.dir, name=f"{base}-orc", role="orchestrator", team=base, task=task)
    peers = []
    for i in range(1, args.builders + 1):
        name = f"{base}-bld{i}"
        peers.append(spawn.Request(runtime="claude", dir=args.dir, name=name, team=base, peer=f"{base}-orc", effort=effort,
                                   worktree=f"{base}/{name}" if args.worktrees else "", shared=not args.worktrees))
    if args.codex:
        peers.append(spawn.Request(runtime="codex", dir=args.dir, name=f"{base}-cdx", team=base, peer=f"{base}-orc",
                                   profile=args.profile, effort=effort, worktree=f"{base}/{base}-cdx" if args.worktrees else "",
                                   shared=not args.worktrees))

    def launch(req):
        try:
            return spawn.spawn(req)
        except spawn.SpawnError as exc:
            return exc

    # The orchestrator first: its peers send it their ONLINE message, so it has to be reachable. Then the peers
    # together, because each launch waits for its agent to be ready and those waits need not queue up.
    first = launch(orchestrator)
    if isinstance(first, spawn.SpawnError):
        print(f"bridge team: {orchestrator.name}: {first}", file=sys.stderr)
        return first.code
    _report(first)
    if not peers:
        return 0
    with ThreadPoolExecutor(max_workers=len(peers)) as pool:
        results = list(pool.map(launch, peers))
    code = 0
    for req, result in zip(peers, results):
        if isinstance(result, spawn.SpawnError):
            print(f"bridge team: {req.name}: {result}", file=sys.stderr)
            code = code or result.code
        else:
            _report(result)
    return code


def cmd_list(_args) -> int:
    agents = {a.name: a for a in registry.all_agents()}
    print(f"{'NAME':<30} {'RT':<6} {'TEAM':<18} {'STATE':<10} {'MODE':<5} WHERE")
    shown = set()
    for s in sorted(claude.sessions(), key=lambda s: s.name):
        if not s.name or not s.reachable and s.name not in agents:
            continue
        a = agents.get(s.name)
        state = "BLOCKED" if s.blocked else ("reachable" if s.reachable else "no-socket")
        print(f"{s.name:<30} {'claude':<6} {(a.team if a else '-'):<18} {state:<10} {(a.mode if a else '-'):<5} {s.cwd}")
        shown.add(s.name)
    for a in sorted(agents.values(), key=lambda a: a.name):
        if a.name in shown:
            continue
        print(f"{a.name:<30} {a.runtime:<6} {a.team or '-':<18} {('live' if a.live else 'DEAD'):<10} {a.mode:<5} {a.cwd}")
    return 0


def cmd_gc(args) -> int:
    if args.dry_run:
        dead = [a for a in registry.all_agents() if not a.live]
    else:
        dead = registry.gc()
        events.emit("gc", removed=len(dead))
    for a in dead:
        print(f"{'would remove' if args.dry_run else 'removed'} {a.name} ({a.runtime}, pid {a.pid})")
    print(f"{len(dead)} dead entr{'y' if len(dead) == 1 else 'ies'}")
    return 0


def cmd_doctor(args) -> int:
    results = doctor.checks()
    if args.loopback:
        results.append(doctor.loopback(directory=args.dir))
    return doctor.render(results)


def cmd_events(args) -> int:
    recs = list(events.read())
    if args.stalls:
        cfg = config.load()
        findings = events.analyse(recs, cfg.int("STALL_MINUTES"), cfg.int("SOFT_CAP"))
        blocked = [s for s in claude.sessions() if s.reachable and s.blocked]
        findings += [f"BLOCKED {s.name} is waiting on a dialog ({s.waiting_for}); open {s.tmux or 'its window'}" for s in blocked]
        print("\n".join(findings) or "no findings")
        return 1 if findings else 0
    import json
    for r in recs[-args.tail:]:
        print(json.dumps(r))
    return 0


def cmd_kick(args) -> int:
    from . import tmux
    agent = registry.get(args.name)
    if agent is None or not agent.pane_id:
        print(f"bridge kick: no registered agent '{args.name}' with a pane", file=sys.stderr)
        return 2
    if tmux.pane_pid(agent.pane_id) != agent.pid:
        print(f"bridge kick: pane {agent.pane_id} no longer runs pid {agent.pid}; refusing to type into an unknown process", file=sys.stderr)
        return 3
    tmux.type_text(agent.pane_id, " ".join(args.text))
    events.emit("kick", to=args.name, pane=agent.pane_id)
    print(f"kicked -> {args.name} ({agent.pane_id})")
    return 0


def cmd_import_legacy(_args) -> int:
    from . import codex, proc
    from .registry import Agent
    legacy = config.claude_home() / "relay" / "bridge-registry.conf"
    if not legacy.is_file():
        print("no legacy registry")
        return 0
    imported = 0
    for line in legacy.read_text().splitlines():
        parts = line.strip().split("=")
        if len(parts) < 4 or not parts[3].isdigit() or not proc.alive(int(parts[3])) or registry.get(parts[0]):
            continue
        thread = codex.thread_by_id(parts[1])
        registry.put(Agent(name=parts[0], runtime="codex", pid=int(parts[3]), tmux_target=parts[2], thread=parts[1],
                           cwd=thread.cwd if thread else "", worktree=thread.cwd if thread else "", started=events.now_iso()))
        imported += 1
    print(f"imported {imported} live legacy entr{'y' if imported == 1 else 'ies'}")
    return 0


def cmd_trust(args) -> int:
    caller = trust.agent_caller()
    if caller:
        print(f"bridge trust: refused. This command is for the human only, and it was run by {caller}. Trusting a "
              f"folder is the human's decision: ask them to run `bridge trust {args.dir}` in their own shell.", file=sys.stderr)
        events.emit("trust", outcome="refused", by=caller, dir=args.dir)
        return trust.REFUSED
    try:
        results = trust.trust_codex(trust.targets(args.dir), dry=args.dry_run)
    except trust.TrustError as exc:
        print(f"bridge trust: {exc}", file=sys.stderr)
        return exc.code
    verb = {"already": "already trusted", "added": "trusted", "updated": "trusted (was set to something else)"}
    for directory, status in results:
        prefix = "would be " if args.dry_run and status != "already" else ""
        print(f"codex : {directory} {prefix}{verb[status]} [{trust.config_path()}]")
    print("claude: not handled here. Claude Code asks once per new git repository and keeps the answer itself; "
          "answer it in the agent's window the first time (the bridge alerts you), or run `claude` there once.")
    if not args.dry_run:
        events.emit("trust", outcome="ok", dirs=[d for d, st in results if st != "already"] or None)
    return 0


def cmd_whoami(_args) -> int:
    me = whoami()
    if not me:
        print("unidentified")
        return 1
    print(f"{me.name} runtime={me.runtime} team={me.team or '-'} depth={me.depth} via={me.source}")
    return 0


def main(argv: Optional[List[str]] = None, prog: str = "bridge") -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if prog in COMPAT:
        from . import compat
        return compat.run(prog, argv)
    if argv and argv[0] == "send":
        return cmd_send(parse_send(argv[1:]))
    args = build_parser().parse_args(argv)
    if args.cmd == "pong":
        events.emit("pong", strict=True, token=args.token)
        print(f"pong {args.token}")
        return 0
    handler = {
        "send": cmd_send, "spawn": cmd_spawn, "resume": cmd_resume, "team": cmd_team, "stop": lambda a: _stop(a),
        "list": cmd_list, "gc": cmd_gc, "doctor": cmd_doctor, "events": cmd_events, "kick": cmd_kick, "whoami": cmd_whoami, "import-legacy": cmd_import_legacy,
        "trust": cmd_trust,
    }[args.cmd]
    return handler(args)


def _stop(args) -> int:
    try:
        agent = spawn.stop(args.name, force=args.force)
    except spawn.SpawnError as exc:
        print(f"bridge stop: {exc}", file=sys.stderr)
        return exc.code
    print(f"stopped {agent.name}")
    return 0
