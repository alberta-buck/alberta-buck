#!/usr/bin/env bash
# Build the peer trace against the archived Solidity without altering its artifacts.
set -euo pipefail

if [[ $# != 2 ]]; then
    echo "Usage: bash $0 FROZEN_RUNTIME NEW_OVERLAY_DIRECTORY" >&2
    exit 2
fi

frozen=$(realpath "$1")
overlay=$(realpath -m "$2")
source_root=$(dirname "$(realpath "$frozen/src")")
libraries="$frozen/lib"
if [[ ! -d "$libraries" ]]; then
    libraries="$source_root/lib"
fi
harness=$(dirname "$(realpath "$0")")/../test/harness

mkdir "$overlay"
mkdir -p "$overlay/test/harness" "$overlay/out"
cp -aL "$frozen/src" "$overlay/"
cp "$frozen/foundry.toml" "$overlay/"
ln -s "$(realpath "$libraries")" "$overlay/lib"
ln -s "$(realpath "$frozen/core")" "$overlay/core"
for artifact in "$frozen"/out/*; do
    [[ ${artifact##*/} == BasketUpkeepPeerTrace.sol ]] && continue
    ln -s "$(realpath "$artifact")" "$overlay/out/${artifact##*/}"
done

# The archived harness predates the peer hook. This changes only inheritance.
sed 's/internal override {/internal virtual override {/' \
    "$frozen/test/harness/BasketUpkeepSignalTrace.sol" > "$overlay/test/harness/BasketUpkeepSignalTrace.sol"
cp "$harness/BasketUpkeepPeerTrace.sol" "$overlay/test/harness/"
forge build --root "$overlay" --out "$overlay/trace-build" "$overlay/test/harness/BasketUpkeepPeerTrace.sol"
cp -r "$overlay/trace-build/BasketUpkeepPeerTrace.sol" "$overlay/out/"
