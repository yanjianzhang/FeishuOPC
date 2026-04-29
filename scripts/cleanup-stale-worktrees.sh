#!/usr/bin/env bash
# Remove B-3 agent worktrees older than 24h.
#
# Usage:
#   scripts/cleanup-stale-worktrees.sh               # apply
#   scripts/cleanup-stale-worktrees.sh --dry-run     # report only
#
# Run from anywhere inside a checkout — we resolve the repo root via
# ``git rev-parse``. Safe to re-run; exits 0 when there is nothing to
# clean (including when the .worktrees/ directory does not exist).
#
# We call ``git worktree remove --force`` rather than ``rm -rf`` so
# the corresponding ``.git/worktrees/<id>`` metadata is cleaned up
# too — a bare ``rm -rf`` would leave orphaned git bookkeeping that
# ``git worktree prune`` would later have to tidy.
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
WORKTREES_DIR="$REPO_ROOT/.worktrees"
DRY_RUN="${1:-}"
AGE_LIMIT_SECONDS="${AGE_LIMIT_SECONDS:-86400}"

if [ ! -d "$WORKTREES_DIR" ]; then
    echo "no .worktrees/ directory; nothing to clean"
    exit 0
fi

now=$(date +%s)
removed=0

for wt in "$WORKTREES_DIR"/*/; do
    [ -d "$wt" ] || continue
    # Portable mtime query: BSD stat on macOS uses -f, GNU stat uses -c.
    if mtime=$(stat -f %m "$wt" 2>/dev/null); then
        :
    else
        mtime=$(stat -c %Y "$wt")
    fi
    age=$((now - mtime))
    if [ "$age" -gt "$AGE_LIMIT_SECONDS" ]; then
        echo "stale: $wt (age ${age}s)"
        if [ "$DRY_RUN" != "--dry-run" ]; then
            # Trailing slash confuses ``git worktree remove`` — strip it.
            git worktree remove --force "${wt%/}" || {
                echo "  warn: git worktree remove failed, leaving in place" >&2
                continue
            }
            removed=$((removed + 1))
        fi
    fi
done

if [ "$DRY_RUN" != "--dry-run" ]; then
    echo "removed $removed stale worktree(s)"
fi
