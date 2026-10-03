#!/bin/sh
# Install agent-bridge: link the CLI + skills, make the minimal config edits. Prints every action.
#   ./install.sh --dry-run      show what would change
#   ./install.sh                install (never replaces files it does not own)
#   ./install.sh --takeover     also move aside pre-existing launchers/skills with the same names
cd "$(dirname "$0")" && exec python3 -m agent_bridge.install "$@"
