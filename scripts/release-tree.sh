#!/bin/sh
# Deletes everything outside the plugin folder: run it only on a throwaway checkout, as CI does.
set -eu
[ "${CI:-}" = true ] || { echo 'release-tree.sh runs only in CI (CI=true)' >&2; exit 1; }
cd "$(dirname "$0")/.."
tmp=$(mktemp -d)
mv claude-plugin "$tmp/plugin"
cp LICENSE CHANGELOG.md .gitattributes "$tmp/"
git rm -r -q --cached .
find . -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
cp -a "$tmp/plugin/." .
cp "$tmp/LICENSE" "$tmp/CHANGELOG.md" "$tmp/.gitattributes" .
rm -rf "$tmp" evals
find hooks -name '*.test.tsx' -delete
