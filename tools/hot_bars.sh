#!/usr/bin/env bash
# The open months of the bar store, kept between runs OUTSIDE git.
#
#   tools/hot_bars.sh restore   before the backfill: lay the open months down
#   tools/hot_bars.sh save      after it: put them back
#
# WHY NOT GIT. Git stores every version of every file it is given, whole: a
# commit that adds one line to forty files stores forty new files and the
# folder listings that point at them. Measured on the week of 2026-09-21, an
# hour's new bars are 650 bytes and the commit that records them in
# per-instrument files 14 KB, 124 MiB a year hourly. Here they are one archive
# on a GitHub release of this repository (a prerelease, one per branch), and
# replacing a release's file costs the repository nothing - release files are
# not part of it. Months enter git once, settled, as plain CSV, and a finished
# year as one Parquet file (jump/bars.py).
#
# THE ARCHIVE is every data/jump/bars/*/*.open.csv, 0.5 to 1.5 MB gzipped.
# Each save uploads a new file named for its run and then deletes the older ones, so a
# save that dies half way leaves the previous archive, not none. Restore takes
# the newest.
#
# NO RELEASE YET: restore keeps whatever open months the checkout holds - the
# CSVs committed before this layout - and the first save creates the release.
# A release that exists but cannot be read FAILS the run: scoring a store
# without its open months would read every recent move as gone, and delivery
# would take their messages down.
#
# Needs GH_TOKEN (the workflow's github.token, with contents: write),
# GITHUB_REPOSITORY and GITHUB_REF_NAME.
set -euo pipefail

BARS=data/jump/bars
TAG="bars-live-${GITHUB_REF_NAME//\//-}"
API="repos/${GITHUB_REPOSITORY}/releases"

retry() {
  local n
  for n in 1 2 3 4; do
    if "$@"; then return 0; fi
    echo "attempt $n failed: $*" >&2
    sleep $((2 ** n))
  done
  return 1
}

# The release's id, empty if it does not exist; fails on any other answer.
release_id() {
  local out
  for n in 1 2 3 4; do
    if out=$(gh api "$API/tags/$TAG" --jq .id 2>&1); then
      echo "$out"; return 0
    fi
    if grep -q "HTTP 404" <<<"$out"; then return 0; fi
    echo "attempt $n: $out" >&2
    sleep $((2 ** n))
  done
  return 1
}

assets() {  # name<TAB>id, newest first
  gh api "$API/$1/assets?per_page=100" \
    --jq 'sort_by(.created_at) | reverse | .[] | select(.name | startswith("bars-live")) | "\(.name)\t\(.id)"'
}

restore() {
  local id newest tmp
  id=$(release_id) || { echo "::error::cannot read release $TAG"; exit 1; }
  if [ -z "$id" ]; then
    echo "no release $TAG yet: keeping the open months the checkout holds"
    return 0
  fi
  newest=$(retry assets "$id" | head -n1 | cut -f1)
  if [ -z "$newest" ]; then
    echo "::error::release $TAG holds no archive"
    exit 1
  fi
  tmp=$(mktemp -d)
  retry gh release download "$TAG" -p "$newest" -D "$tmp" --clobber
  # The release is the newer truth: CSVs a checkout still carries from before
  # this layout must not be read beside it.
  find "$BARS" -mindepth 2 -maxdepth 2 -name '*.open.csv' -delete
  tar -xzf "$tmp/$newest"
  echo "restored $(tar -tzf "$tmp/$newest" | wc -l) open months from $TAG/$newest"
}

save() {
  local id name tmp
  # None on disk is a store that was never laid down, not a quiet month: coins
  # trade every hour. Uploading that would replace the last good archive with
  # an empty one, and the next run would read every recent move as gone.
  if [ -z "$(find "$BARS" -mindepth 2 -maxdepth 2 -name '*.open.csv' -print -quit)" ]; then
    echo "::error::no open months under $BARS: not saved, the release keeps its last archive"
    exit 1
  fi
  tmp=$(mktemp -d)
  name="bars-live-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}.tar.gz"
  find "$BARS" -mindepth 2 -maxdepth 2 -name '*.open.csv' -print0 | sort -z \
    | tar --null -T - -czf "$tmp/$name"
  id=$(release_id) || { echo "::error::cannot read release $TAG"; exit 1; }
  if [ -z "$id" ]; then
    retry gh release create "$TAG" --prerelease --latest=false --target "$GITHUB_SHA" \
      --title "Open months of the bar store ($GITHUB_REF_NAME)" \
      --notes "Written by every hourly run (tools/hot_bars.sh); not a release of the code."
    id=$(release_id)
  fi
  retry gh release upload "$TAG" "$tmp/$name"
  retry assets "$id" | tail -n +2 | while IFS=$'\t' read -r old old_id; do
    [ "$old" = "$name" ] && continue
    gh api -X DELETE "$API/assets/$old_id" || echo "could not delete $old; the next save will" >&2
  done
  echo "saved $(tar -tzf "$tmp/$name" | wc -l) open months to $TAG/$name"
}

"$@"
