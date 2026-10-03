"""`bridge trust <dir>`: the human approves a directory for Codex once, so a spawn there is not held up by Codex's
folder-trust dialog.

Codex (0.160) ignores a command-line trust override for a git repository, and it saves an answer given in the dialog
into the profile file that was active, so another profile asks again. The decision therefore has to be in the base
config.toml, which every profile is layered on.

Only the human may run this: a folder-trust dialog is a decision an agent must not take for itself. Claude Code keeps
its own answer (one per git repository) in a file its running sessions rewrite; the bridge leaves that one alone."""
from __future__ import annotations

import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import config, gitutil, proc
from .identity import IdentityConflict, whoami

try:
    import tomllib
except ImportError:  # python < 3.11
    tomllib = None

AGENT_CMD = re.compile(r"^(\S*/)?(claude|codex)(\s|$)")
TRUSTED = 'trust_level = "trusted"'
REFUSED = 77


class TrustError(Exception):
    def __init__(self, detail: str, code: int = 1):
        super().__init__(detail)
        self.code = code


def agent_ancestor(rows: Optional[Dict[int, tuple]] = None) -> Optional[str]:
    """The agent process this command runs under, if any: 'claude (pid 123)'. Catches agents the registry does not
    know, such as a Codex the human started by hand."""
    rows = rows if rows is not None else proc.table()
    for pid in proc.ancestors(rows=rows)[1:]:
        match = AGENT_CMD.match(rows.get(pid, (0, ""))[1].strip())
        if match:
            return f"{match.group(2)} (pid {pid})"
    return None


def agent_caller() -> Optional[str]:
    try:
        me = whoami()
    except IdentityConflict as exc:
        return str(exc)
    if me:
        return f"'{me.name}' ({me.source})"
    if int(os.environ.get("AGENT_BRIDGE_DEPTH", "0") or 0) > 0:
        return "a bridge-spawned agent (AGENT_BRIDGE_DEPTH)"
    return agent_ancestor()


def targets(directory: str) -> List[str]:
    """What Codex has to trust for an agent in `directory`: its repository, which for a linked worktree is the main
    one. Checked on Codex 0.160: that entry covers the repository's subfolders and every worktree of it, while a
    trusted plain parent folder does not cover a repository inside it. Outside git it is the directory itself."""
    path = Path(directory).expanduser().resolve()
    if not path.is_dir():
        raise TrustError(f"{path} is not a directory", code=2)
    return [gitutil.main_root(str(path)) or gitutil.toplevel(str(path)) or str(path)]


def _key(directory: str) -> str:
    """`directory` as a TOML basic string."""
    out = []
    for ch in directory:
        if ch in '"\\':
            out.append("\\" + ch)
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _parsed_level(text: str, directory: str) -> Optional[str]:
    if tomllib is None:
        return None
    try:
        entry = tomllib.loads(text).get("projects", {}).get(directory)
    except tomllib.TOMLDecodeError as exc:
        raise TrustError(f"{config_path()} is not valid TOML ({exc}); fix it before trusting a folder") from exc
    return entry.get("trust_level") if isinstance(entry, dict) else None


def _trust_one(lines: List[str], directory: str) -> str:
    header = f"[projects.{_key(directory)}]"
    for i, line in enumerate(lines):
        if line.strip() != header:
            continue
        j = i + 1
        while j < len(lines) and not lines[j].lstrip().startswith("["):
            name, sep, value = lines[j].partition("=")
            if sep and name.strip() == "trust_level":
                if value.strip() == '"trusted"':
                    return "already"
                lines[j] = TRUSTED
                return "updated"
            j += 1
        lines.insert(i + 1, TRUSTED)
        return "added"
    if lines and lines[-1].strip():
        lines.append("")
    lines += [header, TRUSTED]
    return "added"


def config_path() -> Path:
    return config.codex_home() / "config.toml"


def _write(path: Path, text: str) -> None:
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".config.toml.")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except OSError:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def trust_codex(directories: List[str], dry: bool = False) -> List[Tuple[str, str]]:
    """Mark each directory trusted in Codex's base config. Returns (directory, already|added|updated)."""
    path = config_path()
    text = path.read_text() if path.is_file() else ""
    lines = text.splitlines()
    results = []
    for directory in directories:
        if _parsed_level(text, directory) == "trusted":
            results.append((directory, "already"))
            continue
        results.append((directory, _trust_one(lines, directory)))
    if dry or all(status == "already" for _, status in results):
        return results
    new = "\n".join(lines) + "\n"
    if tomllib is not None:
        try:
            tomllib.loads(new)
        except tomllib.TOMLDecodeError as exc:
            raise TrustError(f"refusing to write {path}: the edit would not parse as TOML ({exc}). "
                             f"Add the [projects] entry by hand.") from exc
    _write(path, new)
    return results
