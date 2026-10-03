# agent-bridge

Local messaging, spawning and supervision for **Claude Code** and **OpenAI Codex CLI** agents on one machine. Claude can talk to Claude, Claude to Codex, and Codex back to Claude. Delivery is push-only: no polling, no heartbeats, no shared files.

```
 bridge spawn codex -d ~/code/app -n app-cdx -t "fix the flaky test"     # new tmux window, focused
 bridge send app-cdx "BATON: yours ..."          # Claude → Codex   (codex queue, liveness-checked)
 bridge send app-orc "DID: ... EVIDENCE: ..."    # Codex → Claude   (Claude peer socket)
 bridge list                                      # who is reachable; who is BLOCKED on a dialog
 bridge events --stalls                           # stalled batons, loops, oversize, errors, blocked
 bridge doctor [--loopback]                       # verify every dependency; optional live round trip
 bridge trust ~/code/app                          # you, once per repo: Codex skips its folder-trust dialog
```

> **Status.** A personal tool, shared as is. It relies on undocumented internals of Claude Code (the peer-messaging socket, its session files and the `CLAUDE_CODE_HARBOR_KITE` flag) and of Codex CLI (its state database and its TUI), so an upgrade of either can break it. `bridge doctor` checks those dependencies. Developed on macOS; last verified on Linux with Claude Code 2.1.289 and Codex CLI 0.160.0.

## Why it exists

Claude Code sessions can already message each other (`ListAgents` / `SendMessage`). Codex can't reach them, and they can't reach Codex. This repo adds the missing legs and the operational guard rails learned from running the two together in daily use:

- **Provenance marker** on every message (`human=false authority=none`), so a peer can never pass itself off as the human or launder an approval.
- **Identity from process ancestry**, never from inherited environment variables, so a child agent cannot sign as its parent.
- **Unique names and team scoping.** An ambiguous name or a cross-team send is refused, never guessed.
- **One writer per git worktree** by default, with `--worktree <branch>` for isolation. Agents sharing a worktree share the index and the stash.
- **Per-team ledgers** in `<repo>/.relay/<team>/`, which is git-excluded.
- **Large messages spill** to a file in the recipient's repo instead of being refused.
- **Every send, launch and failure is logged** in `~/.local/state/agent-bridge/events.jsonl`. The log holds hashes and sizes, never message bodies.
- **Depth guard**: human → orchestrator → builder is the maximum.
- **`bridge doctor`** checks the undocumented internals this relies on: the Claude peer socket and session files, and the Codex state DB schema. Run it after every Claude or Codex upgrade.

## Requirements

- macOS or Linux, `python3` ≥ 3.9 (stdlib only), `tmux`, `git`.
- Claude Code with the peer-messaging listener enabled. The installer sets `CLAUDE_CODE_HARBOR_KITE=1` in `~/.claude/settings.json`.
- Codex CLI with `codex queue`.

## Install

```sh
git clone https://github.com/OrbitingBucket/agent-bridge.git ~/dev/agent-bridge
cd ~/dev/agent-bridge
./install.sh --dry-run      # shows every change
./install.sh                # or --takeover to replace older launchers with the same names
bridge doctor
```

The installer makes these changes, each one printed and reversible with `./uninstall.sh`:
- Links `bridge`, plus the compatibility names `agent-send`, `codex-send`, `codex-relay`, `claude-relay`, `team-up` and `agent-kick`, into `~/.local/bin`.
- Links the skills into `~/.claude/skills` and `~/.codex/skills`.
- Adds `CLAUDE_CODE_HARBOR_KITE=1` and `Bash(bridge:*)` to Claude settings.
- Adds a marked block to `~/.codex/AGENTS.md` and an allow rule for `bridge` to `~/.codex/rules/default.rules`.
- Creates `~/.config/agent-bridge/config`.

## Configure

`~/.config/agent-bridge/config` uses `KEY=VALUE` lines; see `config/config.example`. Machine- or company-specific setup belongs in a **pre-launch hook**, not in this repo. A hook is an executable in `~/.config/agent-bridge/pre-launch.d/`, and each `KEY=VALUE` line it prints is exported into the new agent's environment (`hooks/pre-launch.d/README.md`). A typical use is refreshing an MCP token.

## Spawning without friction

- **Brief at launch.** `bridge spawn … -t "<brief>"` (or `--task-file brief.md`, `-` for stdin) hands the agent its first brief with the launch, so it starts working instead of reporting ONLINE and waiting. A file avoids shell quoting of `$`, backticks and quotes.
- **Launch together.** Each spawn waits until its agent is ready, so an orchestrator issues its spawns in one turn. `bridge team` starts the orchestrator first and the builders and Codex concurrently.
- **Effort per lane.** `--effort low|medium|high` (also on `team` and `resume`), with defaults in `CLAUDE_EFFORT` / `CODEX_EFFORT`. Without it a spawned agent runs at your own interactive setting, which is usually the maximum.
- **Folder trust.** Both runtimes ask once before working in a new repository, and the bridge never answers for you. Run `bridge trust <dir>` once per repository to settle it for Codex ahead of time; it covers the repository's subfolders and worktrees, under every profile. It refuses to run for an agent (exit 77). Claude Code asks once per new git repository and keeps that answer itself.
- **Dialog alert.** When a dialog does hold a launch, every attached tmux client gets a status-line message naming the window, and that window is highlighted in the status bar until the launch resolves. This matters with `FOCUS=0`, where the window is not brought to the front.

## Layout

| Path | What |
|---|---|
| `bin/bridge` | CLI entry; `bin/<old-name>` are compatibility aliases |
| `agent_bridge/` | `messaging` (send/resolve/spill), `spawn`, `registry` (locked JSON), `identity`, `claude` / `codex` / `tmux` transports, `events` (log + stall analysis), `trust`, `doctor`, `install`, `compat` |
| `protocol/PROTOCOL.md` | The protocol both runtimes follow (marker, BATON/FYI, briefs, disagreement, exit codes) |
| `skills/` | Claude skills `agent-bridge` and `relay`; Codex skill `agent-bridge` |
| `tests/` | `python3 -m unittest discover -s tests`. Uses fake Claude sessions, a fake socket server and a fake `codex` |

## Known limits

- **The Codex → Claude leg uses Claude Code's undocumented peer socket.** `bridge doctor` checks it. The planned replacement is a Claude Channels MCP server.
- **Codex readiness and thread naming rely on its TUI.** The launcher waits for the composer, never types into a dialog (folder trust, hook trust, update prompt), and closes the window on timeout with the last screen lines.
- **Codex 0.160 ignores the command-line trust override for a git repository**, and it saves an answer given in the dialog into the profile file that was active. `bridge trust` writes the base `config.toml` instead, which every profile is layered on.
- **A Codex sandbox in `workspace-write` may deny the socket connect** (`bridge send` exit 8, `eperm`). Approve the escalation once; with the installed allow rule, Codex can run `bridge` without asking.

## License

MIT. See [LICENSE](LICENSE).
