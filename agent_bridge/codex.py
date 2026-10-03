from __future__ import annotations

import re
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from . import config, proc

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
REQUIRED_THREAD_COLUMNS = ("id", "name", "cwd", "archived", "updated_at", "created_at")
READY_MARKERS = ("Ask Codex to do anything",)
DIALOG_MARKERS = ("trust this folder", "folder access", "press enter to continue", "enter continue", "hooks need review", "do you trust", "skip until next version", "update available")
_CODEX_CMD = re.compile(r"^(\S*/)?codex(\s|$)")


@dataclass(frozen=True)
class Thread:
    id: str
    name: str
    cwd: str


class QueueError(Exception):
    pass


def state_db() -> Optional[Path]:
    dbs = sorted(config.codex_home().glob("state_*.sqlite"), key=lambda p: p.stat().st_mtime)
    return dbs[-1] if dbs else None


def _query(sql: str, args: tuple) -> list:
    db = state_db()
    if not db:
        return []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def schema_problems() -> List[str]:
    db = state_db()
    if not db:
        return [f"no state_*.sqlite under {config.codex_home()}"]
    cols = {row[1] for row in _query("pragma table_info(threads)", ())}
    missing = [c for c in REQUIRED_THREAD_COLUMNS if c not in cols]
    return [f"{db.name}: threads table lacks {', '.join(missing)}"] if missing else []


def thread_by_id(thread_id: str) -> Optional[Thread]:
    rows = _query("select id, coalesce(name,''), cwd from threads where id=?", (thread_id,))
    return Thread(*rows[0]) if rows else None


def threads_named(name: str) -> List[Thread]:
    rows = _query(
        "select id, coalesce(name,''), cwd from threads where name=? and archived=0 order by updated_at desc",
        (name,),
    )
    return [Thread(*r) for r in rows]


def thread_ids() -> set:
    return {r[0] for r in _query("select id from threads", ())}


def new_thread(name: str, cwd: str, exclude: set) -> Optional[Thread]:
    """The thread this launch created: named `name`, in `cwd`, and absent from the pre-launch snapshot."""
    rows = _query(
        "select id, coalesce(name,''), cwd from threads where name=? and cwd=? order by created_at desc",
        (name, cwd),
    )
    fresh = [Thread(*r) for r in rows if r[0] not in exclude]
    return fresh[0] if len(fresh) == 1 else None


def codex_pids_in(cwd: str) -> List[int]:
    pids = []
    for pid, (_, cmd) in proc.table().items():
        if _CODEX_CMD.match(cmd.strip()) and proc.cwd_of(pid) == cwd:
            pids.append(pid)
    return pids


def available() -> bool:
    return shutil.which("codex") is not None


def queue(thread_id: str, text: str) -> None:
    try:
        res = subprocess.run(["codex", "queue", "--thread", thread_id, "--message", text], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise QueueError(f"codex queue did not complete: {exc}") from exc
    if res.returncode != 0:
        raise QueueError((res.stderr or res.stdout).strip() or f"codex queue exited {res.returncode}")


def _screen_tail(pane_text: str, lines: int = 14) -> str:
    return "\n".join([l for l in pane_text.splitlines() if l.strip()][-lines:])


def dialog_open(pane_text: str) -> bool:
    low = _screen_tail(pane_text).lower()
    return any(m in low for m in DIALOG_MARKERS)


def is_ready(pane_text: str) -> bool:
    return any(m in _screen_tail(pane_text) for m in READY_MARKERS) and not dialog_open(pane_text)
