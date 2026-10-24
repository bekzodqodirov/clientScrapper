#!/usr/bin/env bash
# Every commit a push sends carries both trailers the history uses
# (CLAUDE.md: Co-Authored-By and Claude-Session), not only the tip: a push of
# several commits was green with an earlier one unsigned.
#
# stdin: git's pre-push lines, «<local ref> <local sha> <remote ref> <remote
# sha>». A new branch (remote sha all zeros) sends what no remote has yet; a
# deletion (local sha all zeros) sends nothing. Run by hand with no input, it
# checks HEAD. Nothing is piped (pre-push.sh's rule: a pipe's status is its
# last command's).
set -euo pipefail
commits=()
saw_input=0
while read -r _local_ref local_sha _remote_ref remote_sha; do
  [ -n "${local_sha:-}" ] || continue
  saw_input=1
  [[ "$local_sha" =~ ^0+$ ]] && continue
  if [[ "${remote_sha:-0}" =~ ^0+$ ]] || ! git cat-file -e "${remote_sha}^{commit}" 2>/dev/null; then
    listed=$(git rev-list "$local_sha" --not --remotes)
  else
    listed=$(git rev-list "${remote_sha}..${local_sha}")
  fi
  while read -r commit; do
    [ -n "$commit" ] && commits+=("$commit")
  done <<<"$listed"
done
if [ "$saw_input" = 0 ]; then
  commits=("$(git rev-parse HEAD)")
fi
missing=0
for commit in "${commits[@]+"${commits[@]}"}"; do
  message=$(git log -1 --format=%B "$commit")
  for trailer in Co-Authored-By Claude-Session; do
    if ! grep -q "^${trailer}:" <<<"$message"; then
      echo "missing ${trailer} trailer: $(git rev-parse --short "$commit")"
      missing=1
    fi
  done
done
exit "$missing"
