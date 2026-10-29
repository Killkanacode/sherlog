#!/usr/bin/env bash
# Recreate the bug/* and noise/* branches on top of the current main from the
# patches in this directory. Run it after changing demo-app/ on main, so that a
# deploy diff only ever shows the injected change.
#
# Patch file names map to branches: bug__orders-keyerror.patch -> bug/orders-keyerror
set -euo pipefail

repo=$(git rev-parse --show-toplevel)
patches="$repo/chaos/bugs"
worktree=$(mktemp -d)
git -C "$repo" worktree add -q --detach "$worktree" main
trap 'git -C "$repo" worktree remove --force "$worktree"' EXIT

for patch in "$patches"/*.patch; do
  name=$(basename "$patch" .patch)
  branch=${name/__//}
  git -C "$worktree" checkout -q -B "$branch" main
  git -C "$worktree" am -q "$patch"
  echo "rebuilt $branch -> $(git -C "$worktree" rev-parse --short HEAD)"
done
git -C "$worktree" checkout -q --detach main
