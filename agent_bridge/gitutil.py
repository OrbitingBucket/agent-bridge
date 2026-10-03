from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

RELAY_DIR = ".relay"


def _git(cwd: str, *args: str) -> Optional[str]:
    try:
        res = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return res.stdout.strip() if res.returncode == 0 else None


def toplevel(cwd: str) -> Optional[str]:
    return _git(cwd, "rev-parse", "--show-toplevel")


def main_root(cwd: str) -> Optional[str]:
    common = _git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not common:
        return None
    return str(Path(common).parent) if common.endswith("/.git") else None


def ensure_excluded(cwd: str, pattern: str = RELAY_DIR + "/") -> None:
    path = _git(cwd, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude")
    if not path:
        return
    exclude = Path(path)
    existing = exclude.read_text() if exclude.is_file() else ""
    if pattern not in existing.splitlines():
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with open(exclude, "a") as fh:
            fh.write(("" if existing.endswith("\n") or not existing else "\n") + pattern + "\n")


def relay_dir(cwd: str, *parts: str) -> Path:
    root = Path(toplevel(cwd) or cwd)
    target = root.joinpath(RELAY_DIR, *parts)
    target.mkdir(parents=True, exist_ok=True)
    ensure_excluded(str(root))
    return target


def add_worktree(repo_dir: str, worktree_root: str, branch: str) -> str:
    main = main_root(repo_dir) or toplevel(repo_dir)
    if not main:
        raise RuntimeError(f"{repo_dir} is not inside a git repository")
    root = Path(worktree_root) if worktree_root else Path(main).parent / "worktrees" / Path(main).name
    target = root.joinpath(*branch.split("/"))
    if target.exists():
        head = _git(str(target), "rev-parse", "--abbrev-ref", "HEAD")
        same_repo = main_root(str(target)) == main
        if not same_repo or head != branch:
            raise RuntimeError(f"{target} exists but is {'another repository' if not same_repo else f'on branch {head}'}, not {branch}")
        return str(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    exists = _git(main, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}") is not None
    args = ["worktree", "add", str(target), branch] if exists else ["worktree", "add", "-b", branch, str(target)]
    res = subprocess.run(["git", "-C", main, *args], capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip())
    return str(target)
