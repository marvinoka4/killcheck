#!/usr/bin/env bash
#
# Clones every eval-set target from targets.json into targets/<name>, checks
# out its pinned commit, and pip-installs it (editable) plus any per-target
# extra_requirements into whatever Python is on PATH.
#
# Run this with the project's own virtualenv active -- the packages need to
# land in the same interpreter that runner.py's test_command ("python3ā€¦")
# will resolve to later, or the suites won't import.
#
# Idempotent: re-running skips a target whose directory already exists and
# is already checked out to the pinned commit, and pip-install is safe to
# repeat. Pass --force to wipe and re-clone everything.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGETS_JSON="$ROOT/targets.json"
TARGETS_DIR="$ROOT/targets"

FORCE=0
if [[ "${1:-}" == "--force" ]]; then
    FORCE=1
fi

if ! command -v git >/dev/null; then
    echo "git is required." >&2
    exit 1
fi
if ! command -v python3 >/dev/null; then
    echo "python3 is required." >&2
    exit 1
fi

mkdir -p "$TARGETS_DIR"

# Emit one TSV line per target: name<TAB>repo<TAB>commit<TAB>project_root<TAB>extra_requirements(comma-joined)
python3 -c "
import json
with open('$TARGETS_JSON') as f:
    targets = json.load(f)
for t in targets:
    extras = ','.join(t.get('extra_requirements', []))
    print(f\"{t['name']}\t{t['repo']}\t{t['commit']}\t{t['project_root']}\t{extras}\")
" | while IFS=$'\t' read -r name repo commit project_root extras; do
    dest="$ROOT/$project_root"

    if [[ "$FORCE" -eq 1 && -d "$dest" ]]; then
        echo "[$name] --force: removing existing checkout"
        rm -rf "$dest"
    fi

    if [[ -d "$dest/.git" ]]; then
        current_sha="$(git -C "$dest" rev-parse HEAD)"
        if [[ "$current_sha" == "$commit" ]]; then
            echo "[$name] already at $commit, skipping clone"
        else
            echo "[$name] present but at $current_sha, expected $commit -- re-fetching"
            git -C "$dest" fetch --quiet origin "$commit"
            git -C "$dest" checkout --quiet "$commit"
        fi
    else
        echo "[$name] cloning $repo @ $commit"
        rm -rf "$dest"
        git clone --quiet --filter=blob:none "$repo" "$dest"
        git -C "$dest" checkout --quiet "$commit"
    fi

    echo "[$name] pip install -e (editable)"
    python3 -m pip install --quiet -e "$dest"

    if [[ -n "$extras" ]]; then
        echo "[$name] pip install extras: $extras"
        # shellcheck disable=SC2086
        python3 -m pip install --quiet $(echo "$extras" | tr ',' ' ')
    fi
done

echo "Done. Verify with: python3 -c \"import json,pathlib; from killcheck.runner import Target, score_target; ...\""
