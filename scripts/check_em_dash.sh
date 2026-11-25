#!/bin/sh
# check-em-dash.sh: fail when an em dash appears in a doc.
#
# Canonical copy: lotek-cadre/guards/check-em-dash.sh. This copy must stay
# identical to it. Keep it in step when the canonical file changes.
#
# Usage: check-em-dash.sh [path ...]
#   No path: scan every tracked AGENTS.md in the current repo.
#   With paths: scan the given files or directories. Use this to widen scope.
#
# The default scope is the agent instruction file, because that is the file this
# rail governs. Widen it per repo once the older prose is clean.
#
# Exit 0 when clean. Exit 1 when it finds an em dash.
set -u

EM=$(printf '\342\200\224')

collect() {
  if [ "$#" -gt 0 ]; then
    for p in "$@"; do
      if [ -d "$p" ]; then
        find "$p" -type f \( -name '*.md' -o -name '*.mdx' -o -name '*.markdown' \) \
          -not -path '*/.git/*' -not -path '*/node_modules/*' -not -path '*/dist/*'
      elif [ -f "$p" ]; then
        printf '%s\n' "$p"
      fi
    done
  elif git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git ls-files -- 'AGENTS.md' '*/AGENTS.md' '**/AGENTS.md'
  else
    find . -type f -name 'AGENTS.md' -not -path '*/.git/*' -not -path '*/node_modules/*'
  fi
}

hits=$(collect "$@" | while IFS= read -r f; do
  [ -n "$f" ] || continue
  [ -f "$f" ] || continue
  grep -n -F "$EM" "$f" 2>/dev/null | sed "s|^|$f:|"
done)

if [ -n "$hits" ]; then
  printf '%s\n' "check-em-dash: FAIL. An em dash is in a doc. Use a full stop, a colon, or a comma." >&2
  printf '%s\n' "$hits" >&2
  exit 1
fi

printf '%s\n' "check-em-dash: clean."
exit 0
