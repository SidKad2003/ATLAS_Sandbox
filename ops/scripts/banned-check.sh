#!/usr/bin/env bash
# Fails if any banned term appears in tracked files. The list lives OUTSIDE the repo;
# its path comes from BANNED_TERMS_FILE (one term per line, case-insensitive).
set -euo pipefail
cd "$(dirname "$0")/../.."
if [ -z "${BANNED_TERMS_FILE:-}" ]; then echo "banned-check: BANNED_TERMS_FILE not set, skipping (provisioned for later)"; exit 0; fi
case "$(realpath "$BANNED_TERMS_FILE")" in "$(pwd)"/*) echo "banned-check: list must live outside the repo"; exit 1 ;; esac
hits=0
while IFS= read -r term || [ -n "$term" ]; do
  term="${term%%#*}"; term="$(echo "$term" | xargs)"; [ -z "$term" ] && continue
  if git grep -n -i -F -- "$term" -- . ':!upstream/LICENSE' >/dev/null 2>&1; then
    echo "banned term found (term index hidden): $(git grep -l -i -F -- "$term" | head -5 | tr '\n' ' ')"; hits=1
  fi
done < "$BANNED_TERMS_FILE"
exit $hits
