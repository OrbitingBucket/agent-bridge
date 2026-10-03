---
name: relay
description: Couple THIS session with another local Claude session as an autonomous duo (orchestrator ↔ builder) over native cross-session messaging (ListAgents/SendMessage) — no relay-file polling, no heartbeats. Use when starting or re-kicking a relay session. Args = role (orchestrator|builder), peer=<peer session name>, optional ledger=<path>, plus an optional initial task.
---

# Relay — autonomous two-session duo over native messaging

You are one half of a two-session pair. The other half is another Claude Code
session on this machine named in the `peer=` arg. **The ONLY thing that wakes
your peer is a `SendMessage` call.** Text you print, files you write, ledger
entries — none of those wake anyone. Conversely, an incoming peer message wakes
YOU automatically, even when you are idle at the prompt. This is verified
behavior, so: no polling loops, no heartbeat sleeps, no watcher scripts.
Deliver your message, end your turn, and trust the wake.

## 0. Preflight (mandatory, in order — do not skip)

Do NOT run filesystem self-checks on your own socket — its directory
(`/tmp/cc-socks/` on macOS, `$XDG_RUNTIME_DIR/cc-socks/` on Linux) is outside
the project dir and even literal reads of it prompt (§3). Reachability is
proven by the handshake instead:

1. **Peer discovery**: run `ListAgents`. Find the peer by name; note its
   `[ref]`. Cross-session sends are rejected with "not an agent in this
   conversation" until you use the full `name [ref]` form once — always
   address first contact (and after any peer restart) as `name [ref]` straight
   from the listing. If a send later fails with "not reachable", re-run
   `ListAgents` first — the peer may have restarted under a new ref.
2. **Handshake**: the builder's first act is an ONLINE message to the
   orchestrator; the orchestrator ACKs (or sends the first brief) in response.
   A send that succeeds proves the RECEIVER is reachable; receiving anything
   proves YOU are. After one round-trip both directions are verified — no
   filesystem checks needed. A failed send is loud and handled via §4.
   **Builder launched with an "Initial task"**: that task IS your first brief.
   Send ONLINE with `BATON: mine` and `NEED: nothing — FYI`, then start on it
   in the same turn. No ACK is coming, so do not wait for one.
3. **Ledger (orchestrator only)**: read the `ledger=` file top to bottom. It
   is the recovery journal — if you are a replacement for a dead orchestrator,
   it is your memory. Append `<UTC> orchestrator ONLINE` using the file tools
   (run a literal `date -u` first for the timestamp). The builder keeps NO
   ledger — a builder's ONLINE signal is its first message, and a replacement
   builder asks the orchestrator to resend the current brief.
4. If the peer is not up yet (fresh start), that's fine for the orchestrator —
   proceed with planning and message the builder when it appears (re-check
   `ListAgents` right before the first handoff, not on a poll loop; if the
   builder is expected but absent, tell the human and end the turn).

## 0b. Peer is a Codex session?

This skill covers Claude<->Claude (native SendMessage/ListAgents). If your peer
is an OpenAI Codex CLI session instead, reach it with `bridge send <name> "..."`
(it replies the same way). Load the `agent-bridge` skill for that leg; the
roles/baton/stall rules below still apply.

## 1. Roles

**Orchestrator** — owns the plan. Decomposes the task, writes briefs with
explicit acceptance criteria, assigns via SendMessage, reviews evidence,
approves or rejects, decides done. Never implements; never races the builder's
lane. Also owns the ledger's structure and the final report to the human.

**Builder** — owns execution. Acknowledges the brief, does the work, reports
back with EVIDENCE (file paths, test output, exit codes — a "done" without the
brief's acceptance evidence is not done). Never invents scope; questions about
the brief go to the orchestrator via SendMessage, never to the human.

## 2. The baton rule (replaces STATUS tokens, turn files, heartbeats)

At every moment exactly ONE side holds the baton. A SendMessage hands it over.

- Hold the baton → you must either act on it or hand it back. Never end a turn
  holding the baton unless you are mid-work with a **harness-tracked** wake
  source armed (a background task whose completion re-invokes you).
- End of every turn, check your last action: did you either (a) send the peer
  a message handing over the baton, or (b) arm a tracked wake (background job,
  Monitor)? If neither — you are about to deadlock the pair; send the message.
- Never end a turn on an intention ("I'll wait for X"). The #1 relay deadlock
  is both sides idle, each believing the other owes a message.
- **Orchestrator SLA timer**: after each handoff that has an expected
  turnaround, arm one tracked background job — a literal
  `sleep <seconds>; echo SLA-CHECK <task-id>` with run_in_background — before
  ending the turn. When it re-invokes you: builder already reported → ignore;
  silence → run §4 liveness. One timer per handoff, no loops, no polling. This
  covers the one failure SendMessage can't: a peer wedged on a permission
  prompt still ACCEPTS deliveries but never processes them.

Message format (every message, both directions):

```
BATON: yours
TASK: <task id / short name>
DID: <what happened since last handoff, one line>
NEED: <what you need from the receiver, or "nothing — FYI">
EVIDENCE: <paths / commands / output refs, when reporting work>
```

## 3. Stall-proofing — each rule kills one class of "waiting for human"

- **Permission prompts are death.** A permission dialog blocks the session until
  a human answers; the peer cannot answer it (that would be permission
  laundering — and it can't see it anyway). Before starting real work, make
  sure the project allowlist covers your expected commands (`/fewer-permission-prompts`
  builds one). If a prompt does appear, the human must clear it — do not design
  workflows that rely on prompt-triggering actions.
- **Even `auto` mode prompts on two things (verified 2026-08-08).**
  (a) Bash commands containing `$VAR` or `$(...)` — the injection heuristic
  flags them ("Contains simple_expansion"). Prefer literal commands; when you
  need a value, print it in one literal call, then use it via file tools or a
  follow-up literal command. (b) File reads/writes OUTSIDE the project
  directory. Keep every relay artifact (ledger, notes, evidence) INSIDE the
  working directory.
- **Never use AskUserQuestion.** Route every question to the orchestrator (or,
  from the orchestrator, decide or park it in the ledger for the human).
- **Never enter plan mode** (no EnterPlanMode/ExitPlanMode) — plan approval
  waits on a human. Plans are markdown in the ledger, approved by the
  orchestrator via message.
- **Long jobs run as harness-tracked background tasks** (Bash with
  `run_in_background`) so completion re-invokes you. NEVER `nohup`/`&` a
  wake-critical process — orphans wake nobody.
- **Waiting on an external condition** (CI, a server, a file): arm a Monitor or
  a background `until` loop — a tracked wake source — then end the turn.
- **Don't block on MCP auth**: if a tool demands interactive auth, report it to
  the orchestrator/ledger and continue without it.

## 4. Liveness & recovery (when a send fails)

1. `SendMessage` fails with "not reachable" → re-run `ListAgents` (new ref?).
2. Still absent → `bridge list`. `BLOCKED` = the peer is wedged on a dialog or
   permission prompt: only the human can clear it — tell them which window.
   `DEAD` / missing = the process is gone.
3. Pid alive but socket gone (a tmp cleaner wiped it) → the peer can still SEND
   but not RECEIVE. `bridge kick <peer> "RELAY KICK: your inbound socket died.
   Read the ledger tail and message <me>."` types into its pane only after
   verifying the pane still runs that pid. Treat it as send-only and ask the
   human to let you respawn it at the next natural boundary.
4. Pid dead → append `PEER DOWN` + current state to the ledger, notify the human
   (PushNotification if available, otherwise your final message), and end the
   turn — or respawn it (§6b) if you are its orchestrator. Do not take over the
   peer's lane on suspicion — only after confirmed death, and say so in the ledger.

## 5. Ledger discipline

The ledger (path from `ledger=`, default `<repo>/.relay/<team>/ledger.md` — one per TEAM, never shared between lanes in the same worktree)
is an append-only journal for RECOVERY and AUDIT — never for signaling. It is
owned and written by the ORCHESTRATOR ONLY; it must live inside the
orchestrator's project directory (out-of-tree paths trigger permission prompts
— see §3). Append one line per meaningful event (`<UTC> <role-observed> <event>`)
plus brief/decision blocks, using the file tools. A replacement orchestrator
must be able to resume from ledger + git state alone. Never rewrite history;
derived work goes in new entries. The builder's durable state is the worktree
itself plus its messages; if the builder needs scratch notes, they go in
`<repo>/.relay/<team>/<your-name>-notes.md`.

## 6. Lifecycle

- **Bootstrap**: orchestrator gets the task (launch arg or from the human) →
  ledger entry with the plan → first brief to builder (baton passes) → builder
  acks in its first message (so the orchestrator knows the channel is live).
  When you spawn the builder yourself, put that first brief in the spawn
  (`-t` / `--task-file`, §6b): the builder then starts at launch and its ONLINE
  message is the ack.
- **Loop**: assign → build → evidence → review → (rework | next assignment).
- **Done**: orchestrator writes the final summary in the ledger, tells the human
  (final message in its own session), and messages the builder it may stand
  down. Sessions stay alive for follow-ups; nobody kills anybody.

## 6b. Spawning & respawning builders (orchestrator only)

You can create your own builders with the `bridge` CLI, which encapsulates every
gotcha (env scrub so the child gets its own identity and socket, the peer-socket
flag, socket verification, stable tmux pane ids, depth guard, one-writer-per-
worktree). NEVER spawn a bare `claude`/`codex` — a bare `claude` inherits your
env and can come up unreachable.

- Claude builder: `bridge spawn claude -d <dir> -n <team>-bld1 --peer <your-name> -t "<brief>"`
- Codex builder: `bridge spawn codex -d <dir> -n <team>-cdx -p build|architect -t "<brief>"`,
  then drive it with `bridge send <name> "..."` (it replies the same way).
- Always pass the first brief at launch. For a real five-part brief, write it
  to a file inside the repo's `.relay/<team>/` and pass `--task-file <path>`:
  no shell quoting, and `$`, backticks and quotes survive.
- Launch all your peers in ONE turn, as parallel tool calls. Each spawn blocks
  until its agent is ready; one after another, the first peer sits idle.
- Set `--effort` per lane (`low`/`medium` for mechanical work, `high` for
  design and review). Without it a peer runs at the human's interactive
  setting, usually the maximum, on a lane that does not need it.
- Writers get their own worktree: `--worktree <team>/<name>`. A second writer in
  a worktree is refused (exit 10) — sharing the git index, stash and branch is
  how agents sweep each other's files into commits. `--shared` only for lanes
  with provably disjoint files.
- Every builder costs the human's account — prefer fewer, better-scoped builders.
- Each new agent is a tmux window, brought to the front of the human's view
  unless they set `FOCUS=0`. If a spawn prints `ACTION NEEDED`, a dialog only
  the human can answer (folder trust) is holding it; the bridge has already
  alerted them. Never type into that window and never run `bridge trust`
  yourself (human only, exit 77) — tell the human and wait for the spawn.
- Teardown: once a builder's work is collected, `bridge stop <name>`.

These commands must be on your permission allowlist (`Bash(bridge:*)`) or they
stall on a prompt. If a spawn command is denied, STOP and tell the human; do not
loop retrying.

**Self-healing:** a builder is dead when `bridge send` exits 3, or `bridge list`
shows it DEAD after a re-check. Note it in the ledger, respawn with the SAME name
(Codex: `bridge resume <name>` keeps its history), and resend its current brief.
Never assign two builders the same lane.

## 7. Site rules

Machine- or company-specific rules (secrets handling, privileged commands,
production access) live in the human's own instructions (CLAUDE.md / AGENTS.md),
not here. They apply unchanged inside relay work.
