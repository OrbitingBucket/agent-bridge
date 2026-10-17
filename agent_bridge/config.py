from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict

DEFAULTS: Dict[str, str] = {
    "TMUX_SESSION": "relay",
    "HUMAN": "the human",
    "TERMINAL": "none",
    "FOCUS": "1",
    "MAX_DEPTH": "2",
    "SOFT_CAP": "6144",
    "SPILL_PREVIEW": "1024",
    "LOG_ROTATE_BYTES": "5242880",
    "CODEX_PROFILE": "build",
    "CODEX_READONLY_PROFILES": "architect",
    "CLAUDE_EFFORT": "",
    "CODEX_EFFORT": "",
    "CLAUDE_MD_SYMLINK": "1",
    "WORKTREE_ROOT": "",
    "STALL_MINUTES": "15",
}


def _home() -> Path:
    return Path(os.environ.get("HOME", str(Path.home())))


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(_home() / ".config")
    return Path(os.environ.get("BRIDGE_CONFIG_DIR", str(Path(base) / "agent-bridge")))


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(_home() / ".local" / "state")
    return Path(os.environ.get("BRIDGE_STATE_DIR", str(Path(base) / "agent-bridge")))


def claude_home() -> Path:
    return Path(os.environ.get("BRIDGE_CLAUDE_HOME", str(_home() / ".claude")))


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(_home() / ".codex")))


def parse_kv(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key.strip()] = value
    return out


@dataclass(frozen=True)
class Config:
    values: Dict[str, str] = field(default_factory=dict)

    def get(self, key: str) -> str:
        return self.values.get(key, DEFAULTS.get(key, ""))

    def int(self, key: str) -> int:
        return int(self.get(key) or 0)

    def flag(self, key: str) -> bool:
        return self.get(key).lower() in ("1", "true", "yes", "on")


def load() -> Config:
    values = dict(DEFAULTS)
    path = config_dir() / "config"
    if path.is_file():
        values.update(parse_kv(path.read_text()))
    for key in DEFAULTS:
        env = os.environ.get("BRIDGE_" + key)
        if env is not None:
            values[key] = env
    return Config(values)
