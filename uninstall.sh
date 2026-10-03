#!/bin/sh
# Remove agent-bridge links and the Codex AGENTS.md block; restore anything moved aside by --takeover.
cd "$(dirname "$0")" && exec python3 -m agent_bridge.install --uninstall "$@"
