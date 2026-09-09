#!/bin/bash
# Conditional build-cache prune for production deploys (issue #396).
#
# Replaces the previous unconditional `docker builder prune -f` in
# docs/deployment.md step 6. That ran on every deploy regardless of
# actual disk pressure and was confirmed (issue #396 Exploration, local
# Colima reproduction) to destroy frontend/Dockerfile's `deps`/`builder`
# intermediate-stage cache every single time it ran, even with zero
# source changes — those stages never appear in the final exported
# image, so plain `-f`'s dangling-only semantics always treat them as
# disposable. That turned a free `node_modules` COPY cache hit into a
# real ~100-114s copy on every deploy for no disk-safety benefit at
# normal usage levels (production sat at 22% used the day this was
# written).
#
# This script only prunes when `/` usage is at or above HIGH_WATER_PCT,
# and when it does, removes cache oldest-first (via increasing
# `--filter until=<age>` cutoffs) until usage drops to LOW_WATER_PCT or
# below. It must not weaken the disk-safety property the original
# every-deploy pruning existed for (docs/deployment.md: cache had once
# silently grown to 29GB / 80% used before any pruning ever ran) — if
# usage is still above the low-water mark after every cutoff has been
# tried, that is a real capacity problem, not a cache problem, and is
# reported as a warning rather than silently accepted.
set -euo pipefail

HIGH_WATER_PCT="${PRUNE_HIGH_WATER_PCT:-75}"
LOW_WATER_PCT="${PRUNE_LOW_WATER_PCT:-40}"
# Oldest-first cutoffs: try the most conservative (oldest-only) prune
# first, widening the age window only if disk usage is still above the
# low-water mark afterward. `until=0h` matches cache of any age and is
# the last-resort full prune (equivalent to the old unconditional -f).
AGE_CUTOFFS=("720h" "168h" "24h" "0h")

disk_used_pct() {
  df -P / | awk 'NR==2 { gsub("%", "", $5); print $5 }'
}

used=$(disk_used_pct)
if [ "$used" -lt "$HIGH_WATER_PCT" ]; then
  echo "[prune] disk at ${used}%, below high-water ${HIGH_WATER_PCT}% -- skipping prune."
  exit 0
fi

echo "[prune] disk at ${used}%, at/above high-water ${HIGH_WATER_PCT}% -- pruning oldest-first until <= ${LOW_WATER_PCT}%."
for cutoff in "${AGE_CUTOFFS[@]}"; do
  used=$(disk_used_pct)
  if [ "$used" -le "$LOW_WATER_PCT" ]; then
    echo "[prune] disk at ${used}%, at/below low-water ${LOW_WATER_PCT}% -- stopping."
    exit 0
  fi
  echo "[prune] disk at ${used}%, pruning build cache older than ${cutoff}..."
  docker builder prune -f --filter "until=${cutoff}"
done

used=$(disk_used_pct)
if [ "$used" -gt "$LOW_WATER_PCT" ]; then
  echo "[prune] WARNING: disk still at ${used}% after pruning all reclaimable build cache (target ${LOW_WATER_PCT}%). This is a real capacity problem, not a cache problem -- investigate directly (docker system df -v, du -sh /var/lib/docker) rather than re-running this script." >&2
fi
