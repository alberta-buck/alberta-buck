#!/usr/bin/env bash
# The five-year worlds (make test-worlds; doc/review/consolidation-plan.org): the canonical economy and the
# household lifecycle mixes, one process each, in parallel, from the repository root inside the venv.
# (Bash 4.3 or later: the nix shell's. macOS's /bin/bash 3.2 has no associative arrays or wait -n.)
#
#   scripts/world-suite.sh                 # all worlds, 1825 days, all at once
#   DAYS=365 scripts/world-suite.sh life-A econ-F    # some worlds, a year
#   JOBS=4 scripts/world-suite.sh          # at most four running at a time (a core each)
#   SEEDS="41404 7 11" JOBS=21 scripts/world-suite.sh   # each world at three seeds: ids <world>-s<seed>
#
# Each writes its run record under $SIM_RUNS (build/runs), its vector under build/sim/<id>.json, and its log
# with a done or failed marker under build/logs. Starts are staggered: the first world on a fresh checkout
# builds the shared identity cache (test/vectors/identity-cache.json), and worlds started together race to
# build it (five of seven reverted InvalidIdentityProof at their bootstrap, 2026-11-14). A world whose
# record is already complete is skipped; remove build/runs/<id> to run it again.
cd "$(dirname "$0")/.." || exit 1
DAYS=${DAYS:-1825}
STAGGER=${STAGGER:-90}
JOBS=${JOBS:-0}             # worlds running at once; 0: all of them
SEEDS=${SEEDS:-}            # seeds to run every world at (ids <world>-s<seed>); empty: each world's own
L=alberta_buck/sim/experiments/economy-lifecycle.toml
E=alberta_buck/sim/experiments/economy.toml
P=agents.HouseholdPopulationAgent
declare -A WORLDS=(
  [econ-F]="--experiment $E"
  [life-A]="--experiment $L"
  [life-A2]="--experiment $L --set scenario.seed=7"
  [life-B]="--experiment $L --set $P.initial_debtors=10 --set $P.young_rate=0.08 --set $P.initial_retirees=3"
  [life-C]="--experiment $L --set $P.initial_retirees=15 --set $P.young_rate=0.02"
  [life-D]="--experiment $L --set $P.initial_investors=24 --set $P.initial_savers=8 --set $P.initial_retirees=3"
  [life-E]="--experiment $L --set $P.young_rate=0"
)
ORDER=(life-A econ-F life-A2 life-B life-C life-D life-E)
[ $# -gt 0 ] && ORDER=("$@")
mkdir -p build/logs build/sim
first=1
start() {     # start ID ARGS...: one world, unless its record is complete
  local id=$1; shift
  if grep -qs '"status": "complete"' "${SIM_RUNS:-build/runs}/$id/manifest.json"; then
    echo "$id: complete; skipped"; return
  fi
  if [ "$JOBS" -gt 0 ]; then
    while [ "$(jobs -rp | wc -l)" -ge "$JOBS" ]; do wait -n; done
  fi
  [ $first -eq 1 ] || sleep "$STAGGER"
  first=0
  rm -f "build/logs/$id.done" "build/logs/$id.failed"
  # The failed marker holds the exit status: 137 is SIGKILL (on Linux, usually the kernel's out-of-memory
  # killer), which leaves no traceback in the log and cannot close the run record.
  ( python -m alberta_buck.sim --backend pyrevm --days "$DAYS" --run-id "$id" --out "build/sim/$id.json" "$@" \
      > "build/logs/$id.log" 2>&1
    rc=$?
    if [ $rc -eq 0 ]; then touch "build/logs/$id.done"
    else echo "exit $rc$([ $rc -eq 137 ] && echo ' (SIGKILL: out of memory?)') at $(date -u +%FT%TZ)" > "build/logs/$id.failed"; fi ) &
  echo "$id: started (log build/logs/$id.log)"
}
for id in "${ORDER[@]}"; do
  args=${WORLDS[$id]:?unknown world $id (known: ${!WORLDS[*]})}
  if [ -z "$SEEDS" ]; then
    start "$id" $args
  else
    for seed in $SEEDS; do start "$id-s$seed" $args --set scenario.seed="$seed"; done
  fi
done
wait
failed=$(ls build/logs/*.failed 2>/dev/null)
[ -z "$failed" ] && exit 0
for f in $failed; do echo "$(basename "$f" .failed): $(cat "$f")"; done
exit 1
