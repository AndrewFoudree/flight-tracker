"""Commit data/ safely when another run may be pushing at the same time.

The CSVs here are append-only, so a conflict between two writers is not a real
conflict: the correct result is the union of both sets of rows. `git pull
--rebase` cannot know that and fails, which is how a completed survey lost its
data after spending the searches.

This fetches the remote state, replays our new rows on top, and retries.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

DATA = Path("data")
ATTEMPTS = 5


def git(*args: str, check: bool = True) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def union_csv(ours: str, theirs: str) -> str:
    """Rows from both sides, theirs first, ours appended if new. Header preserved."""
    our_lines = ours.splitlines()
    their_lines = theirs.splitlines()
    if not their_lines:
        return ours
    header = their_lines[0]
    seen = set(their_lines[1:])
    merged = their_lines[1:]
    for line in our_lines[1:]:
        if line and line not in seen:
            seen.add(line)
            merged.append(line)
    return "\n".join([header, *merged]) + "\n"


def main(message: str) -> int:
    snapshot = {p: p.read_text(encoding="utf-8") for p in sorted(DATA.glob("*")) if p.is_file()}
    if not snapshot:
        print("nothing in data/ to commit")
        return 0

    for attempt in range(1, ATTEMPTS + 1):
        git("fetch", "origin", "main")
        git("reset", "--hard", "origin/main")

        for path, ours in snapshot.items():
            if path.suffix == ".csv" and path.exists():
                path.write_text(union_csv(ours, path.read_text(encoding="utf-8")), encoding="utf-8")
            else:
                # routes.json and alert_state.json are regenerated wholesale, so
                # the run that just produced them is authoritative.
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(ours, encoding="utf-8")

        git("add", "data/")
        if not git("diff", "--staged", "--name-only").strip():
            print("data/ unchanged after merge, nothing to commit")
            return 0
        git("commit", "-m", message)

        push = subprocess.run(["git", "push", "origin", "HEAD:main"], capture_output=True, text=True)
        if push.returncode == 0:
            print(f"pushed on attempt {attempt}")
            return 0
        print(f"attempt {attempt}: push rejected, another writer got there first; retrying")

    print("could not push after retries", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "data update"))
