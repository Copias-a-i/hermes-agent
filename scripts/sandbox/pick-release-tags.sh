#!/usr/bin/env bash
# Pick the release tags the install/update E2E should update FROM.
#
# Emits a JSON array of tag names on stdout, suitable for a GitHub Actions
# matrix (`fromJSON`). Choosing at runtime rather than hardcoding keeps the
# matrix honest as releases land: a pinned list silently stops covering the
# newest release the day after it ships, and pins the "oldest" forever even
# after it stops being a version anyone still runs.
#
# Selection: the newest tag, the oldest tag, and evenly spaced tags in between.
# Newest catches "did the last release break updating?", oldest is the longest
# upgrade jump anyone can still make, and the spread samples the migrations in
# between (config-schema bumps, venv layout changes, dependency floors).
#
# Usage:
#   scripts/sandbox/pick-release-tags.sh [--count N] [--repo DIR]
#
#   --count   how many tags to emit (default 5, minimum 1). Fewer tags than
#             requested emits all of them.
#   --repo    repository to read tags from (default: this checkout).
#
# Reads tags from the local checkout, so it needs one fetched with tags
# (actions/checkout with fetch-depth: 0, or `fetch-tags: true`). A shallow
# checkout has no tags and this exits non-zero rather than silently emitting an
# empty matrix.
#
# Only vYYYY.M.D[.N] release tags are considered; the repo also carries
# backup/* and one-off tags that are not releases.

set -euo pipefail

COUNT=5
# Default to the repository containing this script, resolved through its real
# path so a symlinked or copied script still reads the checkout it lives in
# rather than whatever repo the caller happens to be standing in.
REPO=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --count)
      [ "$#" -ge 2 ] || { echo 'error: --count needs a value' >&2; exit 1; }
      COUNT="$2"; shift 2 ;;
    --repo)
      [ "$#" -ge 2 ] || { echo 'error: --repo needs a value' >&2; exit 1; }
      REPO="$2"; shift 2 ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "error: unknown argument: $1" >&2; exit 1 ;;
  esac
done
case "$COUNT" in
  ''|*[!0-9]*) echo "error: --count must be a positive integer: $COUNT" >&2; exit 1 ;;
esac
[ "$COUNT" -ge 1 ] || { echo 'error: --count must be at least 1' >&2; exit 1; }

# Resolve the script's own location through symlinks, then ask git which
# worktree that path belongs to. Deriving the repo from the script rather than
# from $PWD means a copied script cannot silently report a different checkout's
# tags, and --show-toplevel keeps it correct when invoked from a subdirectory.
if [ -z "$REPO" ]; then
  script_path="${BASH_SOURCE[0]}"
  if command -v readlink >/dev/null 2>&1; then
    script_path="$(readlink -f "$script_path" 2>/dev/null || printf '%s' "$script_path")"
  fi
  script_dir="$(cd "$(dirname "$script_path")" && pwd)"
  REPO="$(git -C "$script_dir" rev-parse --show-toplevel 2>/dev/null || printf '%s' "$script_dir")"
fi

# Sort numeric version components without GNU sort -V so this remains usable by
# the Bash 3.2 shipped with macOS as well as Linux and Git Bash. Keep the git
# command outside process substitution: Bash otherwise exposes the consumer's
# status, not a failed producer's. Exit 3 is reserved for a successful listing
# with no eligible tags; failures to list or process tags keep their genuine
# non-3 status.
tag_list=$(mktemp "${TMPDIR:-/tmp}/pick-release-tags.XXXXXX") || exit 1
eligible_tags=$(mktemp "${TMPDIR:-/tmp}/pick-release-tags.XXXXXX") || exit 1
sorted_tags=$(mktemp "${TMPDIR:-/tmp}/pick-release-tags.XXXXXX") || exit 1
cleanup() {
  rm -f "$tag_list" "$eligible_tags" "$sorted_tags"
}
trap cleanup EXIT HUP INT TERM

if git -C "$REPO" tag --list 'v*' > "$tag_list"; then
  :
else
  git_status=$?
  echo "error: failed to list release tags in $REPO" >&2
  exit "$git_status"
fi

# grep status 1 means no matching lines, which is the typed no-candidates
# condition after the successful git listing above. Any other grep error is
# genuine and must not be converted to exit 3.
if grep -E '^v[0-9]{4}\.[0-9]+\.[0-9]+(\.[0-9]+)?$' "$tag_list" > "$eligible_tags"; then
  :
else
  grep_status=$?
  if [ "$grep_status" -ne 1 ]; then
    echo "error: failed to filter release tags in $REPO" >&2
    exit "$grep_status"
  fi
fi
LC_ALL=C sort -t. -k1.2n -k2,2n -k3,3n -k4,4n "$eligible_tags" > "$sorted_tags"

# Read with Bash 3-compatible primitives: macOS still ships Bash 3.2, which
# lacks `mapfile`, while Git for Windows and Ubuntu also support this loop.
tags=()
while IFS= read -r tag; do
  [ -n "$tag" ] && tags+=("$tag")
done < "$sorted_tags"

total="${#tags[@]}"
if [ "$total" -eq 0 ]; then
  echo "error: no release tags found in $REPO" >&2
  echo '       A shallow clone has no tags: fetch with tags (actions/checkout' >&2
  echo '       with fetch-depth: 0, or fetch-tags: true).' >&2
  # Exit 3 is a typed, recoverable answer for callers that deliberately
  # support a repository bootstrap state with no eligible release tags.
  # Keep all other picker failures non-zero-but-not-3 so CI never mistakes a
  # broken git invocation or invalid input for an honest coverage gap.
  exit 3
fi

if [ "$total" -le "$COUNT" ]; then
  picked=("${tags[@]}")
elif [ "$COUNT" -eq 1 ]; then
  # One slot means the newest release; there is no span to spread across.
  picked=("${tags[$((total - 1))]}")
else
  # Evenly spaced indices across [0, total-1], endpoints included, so the
  # oldest and newest are always present and the rest are spread between them.
  picked=()
  for slot in $(seq 0 $((COUNT - 1))); do
    # Round to nearest rather than truncate, so the spacing does not bunch
    # toward the oldest end.
    index=$(( (slot * (total - 1) * 2 + (COUNT - 1)) / ((COUNT - 1) * 2) ))
    candidate="${tags[$index]}"
    # Guard against a duplicate if rounding lands twice on the same tag.
    case " ${picked[*]-} " in
      *" $candidate "*) continue ;;
    esac
    picked+=("$candidate")
  done
fi

printf '['
for i in "${!picked[@]}"; do
  [ "$i" -eq 0 ] || printf ','
  printf '"%s"' "${picked[$i]}"
done
printf ']\n'
