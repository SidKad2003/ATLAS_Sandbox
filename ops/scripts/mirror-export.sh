#!/usr/bin/env bash
# Build the open-source mirror branch (atlas-main) from main as ONE sanitised commit.
#  - internal identifiers are replaced using a LOCAL map file that is never committed
#    (default .git/info/mirror-map; one "internal<TAB>public" pair per line, literal text)
#  - the export is refused if any internal value is still present
#  - author and committer are a neutral mirror identity, so no internal address reaches the mirror
# Usage: bash ops/scripts/mirror-export.sh [--fresh]     (--fresh: start a new history with no parent)
# Push afterwards with: git push atlas   (the local pre-push guard only lets atlas-main through)
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
MAP="${MIRROR_MAP_FILE:-.git/info/mirror-map}"
SRC_REF="${MIRROR_SOURCE_REF:-main}"
BRANCH="${MIRROR_BRANCH:-atlas-main}"
NAME="${MIRROR_AUTHOR_NAME:-ATLAS Sandbox maintainers}"
EMAIL="${MIRROR_AUTHOR_EMAIL:-atlas-sandbox@users.noreply.github.com}"
FRESH="${1:-}"
[ -f "$MAP" ] || { echo "mirror map $MAP not found (it is local only and must not be committed)"; exit 1; }
while IFS=$'\t' read -r from _ || [ -n "$from" ]; do   # the mirror identity must not contain any internal value
  [ -z "$from" ] && continue; case "$from" in \#*) continue ;; esac
  case "$NAME $EMAIL" in *"$from"*) echo "mirror identity contains an internal value"; exit 1 ;; esac
done < "$MAP"

WORK="$(mktemp -d)"; IDX="$WORK.index"   # git needs a fresh, non-existent index path
trap 'rm -rf "$WORK" "$IDX"' EXIT
git archive "$SRC_REF" | tar -x -C "$WORK"

# apply literal replacements to every text file
while IFS=$'\t' read -r from to || [ -n "$from" ]; do
  [ -z "$from" ] && continue
  case "$from" in \#*) continue ;; esac
  esc_from=$(printf '%s' "$from" | sed 's/[][\.*^$|/]/\\&/g')
  esc_to=$(printf '%s' "$to" | sed 's/[\&|/]/\\&/g')
  { grep -rIlF -- "$from" "$WORK" 2>/dev/null || true; } | while read -r f; do sed -i "s/$esc_from/$esc_to/g" "$f"; done
done < "$MAP"

# refuse to export if anything internal survived
leaks=0
while IFS=$'\t' read -r from _ || [ -n "$from" ]; do
  [ -z "$from" ] && continue; case "$from" in \#*) continue ;; esac
  if grep -rIqF -- "$from" "$WORK"; then echo "LEAK: an internal value is still present (map line hidden)"; leaks=1; fi
done < "$MAP"
[ "$leaks" -eq 0 ] || { echo "export aborted"; exit 1; }

# commit the sanitised tree onto the mirror branch
src_short=$(git rev-parse --short "$SRC_REF")
GIT_INDEX_FILE="$IDX" git --work-tree="$WORK" add -A
# keep executable bits from the source (lost when extracting on some file systems)
git ls-tree -r "$SRC_REF" | awk '$1=="100755"{print $4}' | while read -r p; do
  GIT_INDEX_FILE="$IDX" git --work-tree="$WORK" update-index --chmod=+x -- "$p" 2>/dev/null || true
done
tree=$(GIT_INDEX_FILE="$IDX" git write-tree)
parent_args=()
if [ "$FRESH" != "--fresh" ] && git rev-parse -q --verify "refs/heads/$BRANCH" >/dev/null; then
  [ "$(git rev-parse "$BRANCH^{tree}")" = "$tree" ] && { echo "mirror already up to date"; exit 0; }
  parent_args=(-p "$(git rev-parse "$BRANCH")")
fi
msg="svc-sim open-source mirror sync ($(date -u +%d\ %b\ %Y))"
commit=$(GIT_AUTHOR_NAME="$NAME" GIT_AUTHOR_EMAIL="$EMAIL" GIT_COMMITTER_NAME="$NAME" GIT_COMMITTER_EMAIL="$EMAIL" \
         git commit-tree "$tree" "${parent_args[@]}" -m "$msg")
git update-ref "refs/heads/$BRANCH" "$commit"
echo "built $BRANCH at $(git rev-parse --short "$commit") from $SRC_REF ($src_short)"
echo "review with: git show --stat $BRANCH   then push with: git push atlas"
