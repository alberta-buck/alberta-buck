#!/bin/bash
# The five-year worlds (make test-worlds; doc/review/consolidation-plan.org): the canonical economy and the
# household lifecycle mixes, one process each, in parallel, from the repository root inside the venv.
#
#   scripts/world-suite.sh                 # all worlds, 1825 days
#   DAYS=365 scripts/world-suite.sh life-A econ-F    # some worlds, a year
#
# Each writes its run record under $SIM_RUNS (build/runs), its vector under build/sim/<id>.json, and its log
# with a done or failed marker under build/logs. Starts are staggered: the first world on a fresh checkout
# builds the shared identity cache (test/vectors/identity-cache.json), and worlds started together race to
# build it (five of seven reverted InvalidIdentityProof at their bootstrap, 2026-11-14). A world whose
# record is already complete is skipped; remove build/runs/<id> to run it again.
cd "$(dirname "$0")/.." || exit 1
DAYS=${DAYS:-1825}
STAGGER=${STAGGER:-90}
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
for id in "${ORDER[@]}"; do
  args=${WORLDS[$id]:?unknown world $id (known: ${!WORLDS[*]})}
  if grep -qs '"status": "complete"' "${SIM_RUNS:-build/runs}/$id/manifest.json"; then
    echo "$id: complete; skipped"; continue
  fi
  [ $first -eq 1 ] || sleep "$STAGGER"
  first=0
  rm -f "build/logs/$id.done" "build/logs/$id.failed"
  ( python -m alberta_buck.sim --backend pyrevm --days "$DAYS" --run-id "$id" --out "build/sim/$id.json" $args \
      > "build/logs/$id.log" 2>&1 && touch "build/logs/$id.done" || touch "build/logs/$id.failed" ) &
  echo "$id: started (log build/logs/$id.log)"
done
wait
ls build/logs/*.failed 2>/dev/null && exit 1
exit 0
