from __future__ import annotations

import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional

from . import config

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


def log_path() -> Path:
    return config.state_dir() / "events.jsonl"


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _rotate(path: Path, limit: int) -> None:
    try:
        if limit > 0 and path.stat().st_size > limit:
            os.replace(path, path.with_suffix(".jsonl.1"))
    except FileNotFoundError:
        pass


def emit(event: str, strict: bool = False, **fields) -> Dict:
    """Append one event. Logging never breaks messaging: a sandbox that forbids the write gets a warning, unless strict."""
    record = {"v": 2, "ts": now_iso(), "event": event}
    record.update({k: v for k, v in fields.items() if v is not None})
    line = json.dumps(record, sort_keys=False) + "\n"
    path = log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path.with_suffix(".lock"), "a") as lock:
            if fcntl:
                fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                with open(path, "a") as fh:
                    fh.write(line)
                _rotate(path, config.load().int("LOG_ROTATE_BYTES"))
            finally:
                if fcntl:
                    fcntl.flock(lock, fcntl.LOCK_UN)
    except OSError as exc:
        if strict:
            raise
        sys.stderr.write(f"bridge: warning: could not write event log {path}: {exc}\n")
    return record


def read(paths: Optional[Iterable[Path]] = None) -> Iterator[Dict]:
    if paths is None:
        base = log_path()
        paths = [base.with_suffix(".jsonl.1"), base]
    for path in paths:
        if not Path(path).is_file():
            continue
        with open(path) as fh:
            for line in fh:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue


def _epoch(ts: str) -> float:
    return time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))


def analyse(records: List[Dict], stall_minutes: int, soft_cap: int, now: Optional[float] = None) -> List[str]:
    """Return human-readable findings: STALL, LOOP, BLOAT, ERRORS."""
    now = now if now is not None else time.time()
    findings: List[str] = []
    sends = [r for r in records if r.get("event") == "send"]
    last_baton: Dict[str, Dict] = {}
    replied_after: Dict[str, float] = {}
    for rec in sends:
        if rec.get("outcome") != "delivered":
            continue
        ts = _epoch(rec["ts"])
        if rec.get("kind") == "BATON":
            last_baton[rec.get("to", "")] = rec
        replied_after[rec.get("from", "")] = ts
    for holder, rec in last_baton.items():
        sent = _epoch(rec["ts"])
        if replied_after.get(holder, 0) < sent and now - sent > stall_minutes * 60:
            findings.append(
                f"STALL  {holder} holds the baton from {rec.get('from')} since {rec['ts']} "
                f"(task {rec.get('task', '-')}) and has sent nothing since"
            )
    crossings: Dict[tuple, List[Dict]] = defaultdict(list)
    for rec in sends:
        if rec.get("outcome") == "delivered" and rec.get("task") not in (None, "-"):
            pair = tuple(sorted((rec.get("from", ""), rec.get("to", ""))))
            crossings[(pair, rec["task"])].append(rec)
    for (pair, task), recs in crossings.items():
        hashes = [r.get("sha") for r in recs]
        if len(recs) >= 6 and len(set(hashes)) < len(hashes):
            findings.append(f"LOOP   task {task} crossed {len(recs)} times between {pair[0]} and {pair[1]} with repeated content")
    big = [r for r in sends if r.get("bytes", 0) > soft_cap]
    if big:
        findings.append(f"BLOAT  {len(big)} message(s) above {soft_cap} B (spilled to files)")
    errors = [r for r in sends if r.get("outcome") == "error"]
    if errors:
        by: Dict[str, int] = defaultdict(int)
        for r in errors:
            by[r.get("error", "?")] += 1
        summary = ", ".join(f"{k}={v}" for k, v in sorted(by.items(), key=lambda kv: -kv[1]))
        findings.append(f"ERRORS {len(errors)} failed send(s): {summary}")
    return findings
