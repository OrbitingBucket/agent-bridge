# pre-launch hooks

Executables in `~/.config/agent-bridge/pre-launch.d/` run (in name order) before every `bridge spawn`.
They receive `BRIDGE_RUNTIME` (claude|codex), `BRIDGE_DIR` and `BRIDGE_AGENT` in their environment.
Every `KEY=VALUE` line they print is exported into the new agent's environment (the launch script that carries
it deletes itself on start). A failing hook prints a note and is skipped; it never blocks the spawn.

Example — refresh a short-lived MCP token and hand it to the agent:

```sh
#!/bin/sh
token=$(my-token-refresh --print) || exit 1
echo "MY_MCP_TOKEN=$token"
```
